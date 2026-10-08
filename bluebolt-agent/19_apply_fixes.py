"""
Step 7: Apply fixes. Triggered by the 'apply-sync-fixes' label
(or workflow_dispatch for manual testing).

Jira: posts a plain factual changelog comment (what changed in the
code) - no judgment, no comparison to the ticket description, never
touches the description field itself (business-owned).

Confluence: ADDITIVE ONLY. Never removes or rewords existing
sections (docs-first workflows mean undocumented-but-planned
functionality is normal, not an error). Only appends new sections
for functionality the code has that the doc doesn't mention yet.
A Python-side safety check aborts the write if any original section
heading would be lost, rather than risk silently dropping content.
"""

import asyncio
import base64
import json
import os
import re

import anthropic
import requests
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
ATLASSIAN_EMAIL = os.getenv("ATLASSIAN_EMAIL")
ATLASSIAN_API_TOKEN = os.getenv("ATLASSIAN_API_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
JIRA_SITE_URL = "https://karthik-agents.atlassian.net"
CONFLUENCE_PAGE_ID = "4751361"
CONFLUENCE_PAGE_URL = f"{JIRA_SITE_URL}/wiki/spaces/~712020e1ca0537a9cd4f209fef57ee150169be/pages/{CONFLUENCE_PAGE_ID}/Login+Flow+-+Technical+Design"

GH_REPOSITORY = os.getenv("GH_REPOSITORY")
if GH_REPOSITORY:
    OWNER, REPO = GH_REPOSITORY.split("/")
else:
    OWNER, REPO = "karthik-m924", "mulesoft-sync-demo-v2"

PR_NUMBER = int(os.getenv("GH_PR_NUMBER", "1"))

TICKET_KEY_PATTERN = re.compile(r"^([A-Z]+-\d+):\s*(.+)$")


async def fetch_pr_title_and_diff():
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    async with streamablehttp_client(GITHUB_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            details = await session.call_tool(
                "pull_request_read",
                {"method": "get", "owner": OWNER, "repo": REPO, "pullNumber": PR_NUMBER},
            )
            title = json.loads(details.content[0].text)["title"]
            diff_result = await session.call_tool(
                "pull_request_read",
                {"method": "get_diff", "owner": OWNER, "repo": REPO, "pullNumber": PR_NUMBER},
            )
            diff_text = diff_result.content[0].text
            return title, diff_text


async def fetch_jira_issue(ticket_key):
    basic_auth = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode()).decode()
    headers = {"Authorization": f"Basic {basic_auth}"}
    async with streamablehttp_client(ATLASSIAN_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "getJiraIssue",
                {"cloudId": JIRA_SITE_URL, "issueIdOrKey": ticket_key},
            )
            return json.loads(result.content[0].text)


async def fetch_confluence_page():
    basic_auth = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode()).decode()
    headers = {"Authorization": f"Basic {basic_auth}"}
    async with streamablehttp_client(ATLASSIAN_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "getConfluencePage",
                {"cloudId": JIRA_SITE_URL, "pageId": CONFLUENCE_PAGE_ID, "includeBody": True},
            )
            return json.loads(result.content[0].text)


async def get_html_format_guide():
    basic_auth = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode()).decode()
    headers = {"Authorization": f"Basic {basic_auth}"}
    async with streamablehttp_client(ATLASSIAN_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "getContentFormatGuide",
                {"toolName": "updateConfluencePage"},
            )
            return result.content[0].text


def generate_jira_comment(pr_title, diff_text, ticket_summary):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Summarize, in plain factual language, what this code change adds, removes,
or modifies. This is a changelog entry for the ticket owner's awareness - NOT a judgment
about whether it matches the ticket, and NOT a request for them to review anything.
Do not mention the ticket description at all. Just state what the code now does
differently, in 2-4 sentences, markdown, no greeting or signature.

Ticket: {ticket_summary}
PR title: {pr_title}

Code diff:
{diff_text}"""

    response = client.messages.create(
        model="claude-opus-4-6", max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def extract_headings(html_or_text):
    html_headings = re.findall(r"<h[1-4][^>]*>(.*?)</h[1-4]>", html_or_text, re.IGNORECASE)
    md_headings = re.findall(r"^#{1,4}\s+(.+)$", html_or_text, re.MULTILINE)
    combined = html_headings + md_headings
    return [re.sub(r"<[^>]+>", "", h).strip() for h in combined]


def generate_additive_confluence_body(pr_title, diff_text, current_page_text, format_guide):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""This Confluence page documents a MuleSoft flow. The code may have added
functionality not yet documented here.

CRITICAL RULES:
1. Do NOT remove, reword, or shorten ANY existing section. Documentation often describes
   planned functionality written before the code - a section describing something not
   yet in the code is normal and must be preserved exactly as-is.
2. ONLY add new section(s) for functionality present in the code diff that is not already
   described anywhere in the existing content.
3. If the diff adds nothing new that isn't already documented, return the existing content
   completely unchanged.

Use only basic HTML tags: <h2>, <p>, <table>/<tr>/<td>, <strong>. No emoji icons, macros, or panels.
Follow this formatting guidance:

{format_guide}

Output ONLY the full page body as HTML (existing content + any new section appended) -
no commentary, no markdown fences.

Existing page content:
{current_page_text}

PR title: {pr_title}

Code diff:
{diff_text}"""

    response = client.messages.create(
        model="claude-opus-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def apply_jira_comment(ticket_key, comment_text):
    basic_auth = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode()).decode()
    headers = {"Authorization": f"Basic {basic_auth}"}
    async with streamablehttp_client(ATLASSIAN_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "addCommentToJiraIssue",
                {
                    "cloudId": JIRA_SITE_URL,
                    "issueIdOrKey": ticket_key,
                    "commentBody": f"**Bluebolt Sync Check - Code Changelog**\n\n{comment_text}",
                    "contentFormat": "markdown",
                },
            )
            print(f"Jira comment result isError: {getattr(result, 'isError', 'unknown')}")
            for block in result.content:
                print(block.text if hasattr(block, "text") else block)


async def apply_confluence_update(new_body):
    basic_auth = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode()).decode()
    headers = {"Authorization": f"Basic {basic_auth}"}
    async with streamablehttp_client(ATLASSIAN_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "updateConfluencePage",
                {
                    "cloudId": JIRA_SITE_URL,
                    "pageId": CONFLUENCE_PAGE_ID,
                    "body": new_body,
                    "contentFormat": "html",
                    "versionMessage": "Auto-updated by Bluebolt sync check (additive only)",
                },
            )
            print(f"Confluence update result isError: {getattr(result, 'isError', 'unknown')}")
            for block in result.content:
                print(block.text if hasattr(block, "text") else block)


def post_confirmation_and_remove_label(message):
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    requests.post(
        f"https://api.github.com/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments",
        headers=headers,
        json={"body": message},
    )
    requests.delete(
        f"https://api.github.com/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/labels/apply-sync-fixes",
        headers=headers,
    )


async def main():
    print("Fetching PR title + diff...")
    pr_title, diff_text = await fetch_pr_title_and_diff()

    match = TICKET_KEY_PATTERN.match(pr_title)
    if not match:
        print("No ticket key found - aborting.")
        return
    ticket_key = match.group(1)

    print("Fetching Jira issue...")
    issue = await fetch_jira_issue(ticket_key)
    summary = issue.get("fields", {}).get("summary", "")

    print("Generating Jira changelog comment...")
    jira_comment = generate_jira_comment(pr_title, diff_text, summary)
    await apply_jira_comment(ticket_key, jira_comment)

    print("Fetching Confluence page...")
    page = await fetch_confluence_page()
    current_body_text = page.get("body", "")

    print("Fetching HTML format guide...")
    format_guide = await get_html_format_guide()

    print("Generating additive Confluence update...")
    new_body = generate_additive_confluence_body(pr_title, diff_text, current_body_text, format_guide)

    original_headings = set(extract_headings(current_body_text))
    new_headings = set(extract_headings(new_body))
    missing = original_headings - new_headings

    confluence_applied = False
    if missing:
        print(f"SAFETY ABORT: Confluence update would drop existing sections: {missing}")
        print("Skipping Confluence write to avoid data loss.")
    else:
        await apply_confluence_update(new_body)
        confluence_applied = True

    print("Posting confirmation comment...")
    confluence_line = (
        f"- \U0001F4D8 Updated [Confluence page]({CONFLUENCE_PAGE_URL}) with new content only "
        f"(existing sections preserved)"
        if confluence_applied
        else f"- \u26A0\uFE0F Confluence update skipped - would have removed existing content, review manually: "
             f"[Confluence page]({CONFLUENCE_PAGE_URL})"
    )
    post_confirmation_and_remove_label(
        f"**\U0001F916 Bluebolt Sync Fixes Applied**\n\n"
        f"- \U0001F3AB Added a changelog comment on [{ticket_key}]({JIRA_SITE_URL}/browse/{ticket_key}) "
        f"(description left untouched - business-owned field)\n"
        f"{confluence_line}"
    )
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())