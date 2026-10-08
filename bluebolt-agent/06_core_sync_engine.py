"""
Step 6 (v5): Core sync engine with Jira AND Confluence checks.
Confluence uses a direct page fetch (not RAG) since this demo
has one known page - see conversation for when RAG would be needed.
"""

import asyncio
import base64
import json
import os
import re

import anthropic
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
CONFLUENCE_PAGE_URL = "https://karthik-agents.atlassian.net/wiki/spaces/~712020e1ca0537a9cd4f209fef57ee150169be/pages/4751361/Login+Flow+-+Technical+Design"

GH_REPOSITORY = os.getenv("GH_REPOSITORY")
if GH_REPOSITORY:
    OWNER, REPO = GH_REPOSITORY.split("/")
else:
    OWNER, REPO = "karthik-m924", "mulesoft-sync-demo"

PR_NUMBER = int(os.getenv("GH_PR_NUMBER", "1"))

TICKET_KEY_PATTERN = re.compile(r"^([A-Z]+-\d+):\s*(.+)$")
STATUS_LINE_PATTERN = re.compile(r"^STATUS:\s*(IN_SYNC|OUT_OF_SYNC)\s*$", re.MULTILINE)


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


async def fetch_atlassian_object(object_url):
    basic_auth = base64.b64encode(f"{ATLASSIAN_EMAIL}:{ATLASSIAN_API_TOKEN}".encode()).decode()
    headers = {"Authorization": f"Basic {basic_auth}"}

    async with streamablehttp_client(ATLASSIAN_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "getTeamworkGraphObject",
                {"cloudId": JIRA_SITE_URL, "objects": [object_url]},
            )
            data = json.loads(result.content[0].text)
            return data["data"]["data"]["objects"][0]["raw"]


def strip_html_tags(html_text):
    # Quick cleanup so Claude sees readable text, not raw markup noise
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def run_consistency_check(source_label, source_content, pr_title, diff_text, extra_context=""):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are reviewing whether a {source_label} document still accurately describes a code change.

{extra_context}

{source_label} content:
{source_content}

Pull request title: {pr_title}

Code diff:
{diff_text}

Respond in EXACTLY this structure, nothing before or after:

STATUS: IN_SYNC
(or STATUS: OUT_OF_SYNC - this line must be the very first line, exactly this format)

### Comparison

| # | Change in Code | Mentioned in {source_label}? | Status |
|---|---|---|---|
| 1 | <specific change, plain language> | <Yes - quote matching phrase / No - missing> | \u2705 or \u274C |

Also check the reverse direction: if {source_label} describes functionality that is
NOT present anywhere in the code diff, add a row for that too, with "Change in Code"
saying "Not found in code" and quoting what the doc describes instead.

Only include rows for changes/features that affect functionality or behavior. Skip
whitespace, line-ending, formatting, and comment-only changes entirely.
If literally everything is non-functional, say so in the verdict instead of
showing an empty or misleading table.

### Verdict
One sentence: in sync or out of sync, and why.

### If out of sync - exact fix
Only include this section when STATUS is OUT_OF_SYNC.
- **Where:** the {source_label} source
- **Change to:** <the exact new text to add or update>
"""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def split_status_and_body(report_text):
    match = STATUS_LINE_PATTERN.search(report_text)
    if not match:
        return "OUT_OF_SYNC", report_text
    status = match.group(1)
    body = STATUS_LINE_PATTERN.sub("", report_text, count=1).strip()
    return status, body


async def post_pr_comment(jira_status, jira_body, ticket_key, confluence_status, confluence_body, confluence_url):
    jira_badge = "\u2705 In Sync" if jira_status == "IN_SYNC" else "\u274C Out of Sync"
    confluence_badge = "\u2705 In Sync" if confluence_status == "IN_SYNC" else "\u274C Out of Sync"
    jira_link = f"{JIRA_SITE_URL}/browse/{ticket_key}"

    body = (
        f"## \U0001F916 Bluebolt Sync Check\n\n"
        f"**\U0001F4C4 Code:** PR #{PR_NUMBER} (`{OWNER}/{REPO}`)\n\n"
        f"| Source | Status | Link |\n"
        f"|---|---|---|\n"
        f"| \U0001F3AB Jira | {jira_badge} | [{ticket_key}]({jira_link}) |\n"
        f"| \U0001F4D8 Confluence | {confluence_badge} | [Login Flow - Technical Design]({confluence_url}) |\n\n"
        f"<details>\n<summary>\U0001F50D Jira comparison details</summary>\n\n{jira_body}\n\n</details>\n\n"
        f"<details>\n<summary>\U0001F50D Confluence comparison details</summary>\n\n{confluence_body}\n\n</details>\n"
    )

    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    async with streamablehttp_client(GITHUB_MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "add_issue_comment",
                {"owner": OWNER, "repo": REPO, "issue_number": PR_NUMBER, "body": body},
            )
            print(f"isError: {getattr(result, 'isError', 'unknown')}")
            for block in result.content:
                print(block.text if hasattr(block, "text") else block)


async def main():
    print("Fetching PR title + diff from GitHub...")
    pr_title, diff_text = await fetch_pr_title_and_diff()
    print(f"PR title: {pr_title}\n")

    match = TICKET_KEY_PATTERN.match(pr_title)
    if not match:
        print("No ticket key found in PR title - skipping.")
        return
    ticket_key = match.group(1)
    print(f"Parsed ticket key: {ticket_key}")

    print("Fetching ticket from Jira...")
    ticket = await fetch_atlassian_object(f"{JIRA_SITE_URL}/browse/{ticket_key}")
    print(f"Ticket summary: {ticket['summary']}\n")

    print("Checking Jira consistency...")
    jira_context = f"""Jira ticket {ticket['key']} - "{ticket['summary']}"
Status: {ticket['jiraStatus']['name']}
Description: {ticket.get('description') or '(no description)'}"""
    jira_report = run_consistency_check("Jira", jira_context, pr_title, diff_text)
    jira_status, jira_body = split_status_and_body(jira_report)
    print(f"Jira status: {jira_status}\n")

    print("Fetching Confluence page...")
    page = await fetch_atlassian_object(CONFLUENCE_PAGE_URL)
    page_text = strip_html_tags(page["content"]["storage"]["value"])
    print(f"Page title: {page['title']}\n")

    print("Checking Confluence consistency...")
    confluence_report = run_consistency_check("Confluence", page_text, pr_title, diff_text)
    confluence_status, confluence_body = split_status_and_body(confluence_report)
    print(f"Confluence status: {confluence_status}\n")

    await post_pr_comment(
        jira_status, jira_body, ticket_key,
        confluence_status, confluence_body, CONFLUENCE_PAGE_URL,
    )


if __name__ == "__main__":
    asyncio.run(main())