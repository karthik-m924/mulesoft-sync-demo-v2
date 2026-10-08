"""
Step 7: Apply fixes. Triggered by the 'apply-sync-fixes' label.
- Jira: adds a flagging COMMENT only, never touches the description
  (business-owned field).
- Confluence: full page content update (developer-owned doc).
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





def generate_jira_comment(pr_title, diff_text, ticket_summary, ticket_description):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""A pull request may have changed functionality not reflected in this Jira ticket.

Ticket summary: {ticket_summary}
Ticket description: {ticket_description or '(none)'}
PR title: {pr_title}

Code diff:
{diff_text}

Write a short, professional Jira comment (3-5 sentences, markdown) flagging to the
ticket owner what the code now does that the description doesn't mention, and asking
them to review whether the description should be updated. Do NOT write a replacement
description - only flag the discrepancy for the business owner to decide on.
Do not include a greeting or signature."""

    response = client.messages.create(
        model="claude-opus-4-6", max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def generate_updated_confluence_body(pr_title, diff_text, current_page_text, format_guide):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""This Confluence page documents a MuleSoft flow. The code has changed.
Produce a FULL corrected version of the page content, in simple HTML, that accurately
reflects the current code. Use only basic tags: <h2>, <p>, <table>/<tr>/<td>, <strong>.
Do NOT use emoji icons, macros, or panels - plain structural HTML only.
Follow this formatting guidance:

{format_guide}

Keep existing sections where still accurate. Remove sections describing functionality
that no longer exists in the code. Add sections for new functionality.
Output ONLY the full replacement page body as HTML - no commentary, no markdown fences.

Current page content:
{current_page_text}

PR title: {pr_title}

Code diff:
{diff_text}"""

    response = client.messages.create(
        model="claude-opus-4-6", max_tokens=1500,
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
                    "commentBody": f"**Bluebolt Sync Check**\n\n{comment_text}",
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
                    "versionMessage": "Auto-updated by Bluebolt sync check",
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
    description = issue.get("fields", {}).get("description", "")

    print("Generating Jira comment...")
    jira_comment = generate_jira_comment(pr_title, diff_text, summary, description)
    await apply_jira_comment(ticket_key, jira_comment)

    print("Fetching Confluence page...")
    page = await fetch_confluence_page()
    current_body_text = page.get("body", "")

    print("Fetching HTML format guide...")
    format_guide = await get_html_format_guide()

    print("Generating updated Confluence body...")
    new_body = generate_updated_confluence_body(pr_title, diff_text, current_body_text, format_guide)
    await apply_confluence_update(new_body)

    print("Posting confirmation comment...")
    post_confirmation_and_remove_label(
        f"**🤖 Bluebolt Sync Fixes Applied**\n\n"
        f"- 🎫 Added a flag comment on [{ticket_key}]({JIRA_SITE_URL}/browse/{ticket_key}) "
        f"for the ticket owner to review (description left untouched - business-owned field)\n"
        f"- 📘 Updated [Confluence page]({CONFLUENCE_PAGE_URL}) to match current code"
    )
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())