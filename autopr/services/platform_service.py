import json
import sys
import traceback
from typing import Optional, Union, Any, Type

import aiohttp
import requests
from aiohttp import ClientSession

from autopr.log_config import get_logger
from autopr.models.artifacts import Issue, Message, PullRequest
from autopr.models.events import EventUnion, LabelEvent, CommentEvent, PushEvent


class PlatformService:
    """
    Service for making API calls to the platform (e.g., GitHub).
    """

    class PRBodySentinel:
        pass

    def __init__(
        self,
        owner: str,
        repo_name: str,
    ):
        self.owner = owner
        self.repo_name = repo_name

        self.log = get_logger(service="publish")

    async def publish_comment(self, text: str, issue_number: int) -> Optional[str]:
        """
        Publish a comment to the issue (pull requests are also issues).

        Parameters
        ----------

        text: str
            The text to comment
        issue_number: Optional[int]
            The issue number to comment on. If None, should comment on the PR.
        """
        raise NotImplementedError

    async def set_title(self, title: str):
        """
        Set the title of the pull request.

        Parameters
        ----------

        title: str
            The title to set
        """
        raise NotImplementedError

    async def get_issues(self, state: str = "open") -> list[Issue]:
        """
        Get a list of issues.

        Parameters
        ----------
        state: str
            The state of the issues to get. Can be "open", "closed", or "all".
        """
        raise NotImplementedError

    async def find_existing_pr(self, head_branch: str, base_branch: str) -> Optional[int]:
        """
        Find an existing pull request.

        Returns
        -------
        Optional[int]
            The pull request number, or None if no PR exists.
        """
        raise NotImplementedError

    async def create_pr(
        self,
        title: str,
        bodies: list[str],
        draft: bool,
        head_branch: str,
        base_branch: str
    ) -> tuple[Optional[int], list[Union[str, Type[PRBodySentinel]]]]:
        """
        Create a pull request.

        Parameters
        ----------
        title: str
            The title of the PR
        bodies: list[str]
            The bodies of the PR
        draft: bool
            Whether to create the PR as a draft
        head_branch: str
            The head branch of the PR
        base_branch: str
            The base branch of the PR
        """
        raise NotImplementedError

    async def update_pr_body(self, pr_number: int, body: str):
        """
        Update the body of the pull request.

        Parameters
        ----------
        pr_number: int
            The PR number
        body: str
            The new body
        """
        raise NotImplementedError

    async def update_pr_title(self, pr_number: int, title: str):
        """
        Update the title of the pull request.

        Parameters
        ----------
        pr_number: int
            The PR number
        title: str
            The new title
        """
        raise NotImplementedError

    async def set_pr_draft_status(self, pr_number: int, is_draft: bool):
        """
        Set the draft status of the pull request.

        Parameters
        ----------
        pr_number: int
            The PR number
        is_draft: bool
            Whether to set the PR as a draft
        """
        raise NotImplementedError

    async def update_comment(self, comment_id: str, body: str):
        """
        Update a comment.

        Parameters
        ----------
        comment_id: str
            The comment ID
        body: str
            The new body
        """
        raise NotImplementedError

    def parse_event(self, event: dict[str, Any], event_name: str) -> EventUnion:
        """
        Parse an event from the platform.

        Parameters
        ----------
        event: dict[str, Any]
            The event to parse
        event_name: str
            The name of the event

        Returns
        -------
        Optional[EventUnion]
            The parsed event, or None if the event is not supported
        """
        raise NotImplementedError


class GitHubPlatformService(PlatformService):
    """
    Publishes the PR to GitHub.

    Sets it as draft while it's being updated, and removes the draft status when it's finalized.
    Adds a shield linking to the action logs, a "Fixes #{issue_number}" link.

    """

    def __init__(
        self,
        token: str,
        owner: str,
        repo_name: str,
    ):
        super().__init__(
            owner=owner,
            repo_name=repo_name,
        )
        self.token = token

        self._pr_node_id: Optional[str] = None

        self._drafts_supported = True

    async def _log_failed_request(
        self,
        reason: str,
        response: aiohttp.ClientResponse,
        request_url: str,
        request_headers: Optional[dict[str, Any]] = None,
        request_params: Optional[dict[str, Any]] = None,
        request_body: Optional[dict[str, Any]] = None,
    ):
        try:
            text = await response.json()
        except json.JSONDecodeError:
            text = await response.text()

        self.log.error(
            reason,
            request_url=request_url,
            request_headers=request_headers,
            request_params=request_params,
            # request_body=request_body,
            response_text=text,
            response_code=response.status,
            response_headers=response.headers,
        )

    def _get_headers(self):
        return {
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        }

    async def find_existing_pr(self, head_branch: str, base_branch: str) -> Optional[int]:
        """
        Returns the PR dict of the first open pull request with the same head and base branches
        """

        url = f'https://api.github.com/repos/{self.owner}/{self.repo_name}/pulls'
        headers = self._get_headers()
        params = {'state': 'open', 'head': f'{self.owner}:{head_branch}', 'base': base_branch}

        async with ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:

                if response.status == 200:
                    prs = await response.json()
                    if prs:
                        return prs[0]['number']

                await self._log_failed_request(
                    'Failed to get pull requests',
                    request_url=url,
                    request_headers=headers,
                    request_params=params,
                    response=response,
                )
        return None

    async def create_pr(
        self,
        title: str,
        bodies: list[str],
        draft: bool,
        head_branch: str,
        base_branch: str
    ) -> tuple[Optional[int], list[Union[str, Type[PlatformService.PRBodySentinel]]]]:
        url = f'https://api.github.com/repos/{self.owner}/{self.repo_name}/pulls'
        headers = self._get_headers()
        data = {
            'head': head_branch,
            'base': base_branch,
            'title': title,
            'body': bodies[0],
        }
        if self._drafts_supported:
            data['draft'] = "true" if draft else "false"

        async with ClientSession() as session:
            async with session.post(url, json=data, headers=headers) as response:
                if response.status == 201:
                    response_json = await response.json()

                elif self._is_draft_error(await response.text()):
                    del data['draft']
                    async with session.post(url, json=data, headers=headers) as second_response:
                        if second_response.status != 201:
                            await self._log_failed_request(
                                'Failed to create pull request',
                                request_url=url,
                                request_headers=headers,
                                request_body=data,
                                response=second_response,
                            )
                            raise RuntimeError('Failed to create pull request')
                        response_json = await second_response.json()
                else:
                    await self._log_failed_request(
                        'Failed to create pull request',
                        request_url=url,
                        request_headers=headers,
                        request_body=data,
                        response=response,
                    )
                    raise RuntimeError('Failed to create pull request')
                self.log.debug('Pull request created successfully',
                               headers=response.headers)
                pr_number = response_json['number']

        comment_ids: list[Union[str, Type[PlatformService.PRBodySentinel]]] = [self.PRBodySentinel]

        # Add additional bodies as comments
        for body in bodies[1:]:
            id_ = await self.publish_comment(body, pr_number)
            if id_ is None:
                raise RuntimeError("Failed to publish progress comment")
                # self.log.error("Failed to publish progress comment")
            comment_ids.append(id_)

        return pr_number, comment_ids

    async def _patch_pr(self, pr_number: int, data: dict[str, Any]):
        url = f'https://api.github.com/repos/{self.owner}/{self.repo_name}/pulls/{pr_number}'
        headers = self._get_headers()

        async with ClientSession() as session:
            async with session.patch(url, json=data, headers=headers) as response:
                if response.status == 200:
                    self.log.debug('Pull request updated successfully')
                    return

                await self._log_failed_request(
                    'Failed to update pull request',
                    request_url=url,
                    request_headers=headers,
                    request_body=data,
                    response=response,
                )

    def _is_draft_error(self, response_text: str):
        response_obj = json.loads(response_text)
        is_draft_error = 'message' in response_obj and \
                         'draft pull requests are not supported' in response_obj['message'].lower()
        if is_draft_error:
            self.log.warning("Pull request drafts error on this repo")
            self._drafts_supported = False
        return is_draft_error

    async def _get_pull_request_node_id(self, pr_number: int) -> str:
        url = f'https://api.github.com/repos/{self.owner}/{self.repo_name}/pulls/{pr_number}'
        headers = self._get_headers()

        async with ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return (await response.json())['node_id']

                await self._log_failed_request(
                    'Failed to get pull request node id',
                    request_url=url,
                    request_headers=headers,
                    response=response,
                )

        raise RuntimeError('Failed to get pull request node id')

    async def set_pr_draft_status(self, pr_number: int, is_draft: bool):
        if not self._drafts_supported:
            return
        if self._pr_node_id is None:
            self._pr_node_id = await self._get_pull_request_node_id(pr_number)

        # sadly this is only supported by graphQL
        if is_draft:
            graphql_query = '''
                mutation ConvertPullRequestToDraft($pullRequestId: ID!) {
                  convertPullRequestToDraft(input: { pullRequestId: $pullRequestId }) {
                    clientMutationId
                  }
                }
            '''
        else:
            graphql_query = '''
                mutation MarkPullRequestReadyForReview($pullRequestId: ID!) {
                  markPullRequestReadyForReview(input: { pullRequestId: $pullRequestId }) {
                    clientMutationId
                  }
                }
            '''
        headers = self._get_headers() | {
            'Content-Type': 'application/json'
        }

        # Update the pull request
        data = {'pullRequestId': self._pr_node_id}
        url = 'https://api.github.com/graphql'
        body = {'query': graphql_query, 'variables': data}

        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=body) as response:
                if response.status == 200:
                    self.log.debug('Pull request draft status updated successfully')
                    return

                await self._log_failed_request(
                    'Failed to update pull request draft status',
                    request_url=url,
                    request_headers=headers,
                    request_body=body,
                    response=response,
                )

        self._drafts_supported = False

    async def update_pr_body(self, pr_number: int, body: str):
        await self._patch_pr(pr_number, {'body': body})

    async def update_pr_title(self, pr_number: int, title: str):
        await self._patch_pr(pr_number, {'title': title})

    async def update_comment(self, comment_id: str, body: str):
        url = f'https://api.github.com/repos/{self.owner}/{self.repo_name}/issues/comments/{comment_id}'
        headers = self._get_headers()

        async with ClientSession() as session:
            async with session.patch(url, json={'body': body}, headers=headers) as response:
                if response.status == 200:
                    self.log.debug('Comment updated successfully')
                    return

                await self._log_failed_request(
                    'Failed to update comment',
                    request_url=url,
                    request_headers=headers,
                    request_body={'body': body},
                    response=response,
                )

    async def publish_comment(self, text: str, issue_number: int) -> Optional[str]:
        url = f'https://api.github.com/repos/{self.owner}/{self.repo_name}/issues/{issue_number}/comments'
        headers = self._get_headers()
        data = {
            'body': text,
        }

        async with ClientSession() as session:
            async with session.post(url, json=data, headers=headers) as response:
                if response.status == 201:
                    self.log.debug('Commented on issue successfully')
                    return (await response.json())['id']

                await self._log_failed_request(
                    'Failed to comment on issue',
                    request_url=url,
                    request_headers=headers,
                    request_body=data,
                    response=response,
                )
        return None

    def _extract_issue(self, issue_json: dict[str, Any]) -> Optional[Issue]:
        url = issue_json['comments_url']
        assert url.startswith('https://api.github.com/repos/'), "Unexpected comments_url"
        self.log.info("Getting issue comments", url=url)
        headers = self._get_headers()
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        comments_json = response.json()
        self.log.info("Got issue comments", comments=comments_json)

        body_message = Message(
            body=issue_json['body'] or "",
            author=issue_json['user']['login'],
        )
        comments_list = [body_message]
        # Get comments
        for comment_json in comments_json:
            comment = Message(
                body=comment_json['body'] or "",
                author=comment_json['user']['login'],
            )
            comments_list.append(comment)

        # Create issue
        return Issue(
            number=issue_json['number'],
            title=issue_json['title'],
            author=issue_json['user']['login'],
            timestamp=issue_json["updated_at"],
            messages=comments_list,
        )

    def _extract_pull_request(self, pr_json: dict[str, Any]) -> Optional[PullRequest]:
        issue = self._extract_issue(pr_json)
        if issue is None:
            return issue
        return PullRequest(
            number=issue.number,
            title=issue.title,
            author=issue.author,
            timestamp=issue.timestamp,
            messages=issue.messages,
            head_branch=pr_json['head']['ref'],
            base_branch=pr_json['base']['ref'],
            base_commit_sha=pr_json['base']['sha'],
        )

    async def get_issues(self, state: str = "open") -> list[Issue]:
        url = f'https://api.github.com/repos/{self.owner}/{self.repo_name}/issues?state={state}'
        headers = self._get_headers()

        async with ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    await self._log_failed_request(
                        'Failed to get issues',
                        request_url=url,
                        request_headers=headers,
                        response=response,
                    )
                    return []

                return [
                    issue
                    for issue_json in await response.json()
                    if (issue := self._extract_issue(issue_json)) is not None
                ]

    def parse_event(self, event: dict[str, Any], event_name: str) -> EventUnion:
        if event_name == 'push':
            return PushEvent(
                branch=event['ref'].split('/')[-1],
            )
        if event['action'] == 'labeled':
            return LabelEvent(
                pull_request=self._extract_pull_request(event['pull_request']) if 'pull_request' in event else None,
                issue=self._extract_issue(event['issue']) if 'issue' in event else None,
                label=event['label']['name'],
            )
        elif event['action'] == 'comment':
            return CommentEvent(
                pull_request=self._extract_pull_request(event['issue']['pull_request']),
                issue=self._extract_issue(event['issue']),
                comment=Message(
                    body=event['comment']['body'],
                    author=event['comment']['user']['login'],
                ),
            )
        raise NotImplementedError(f"Unknown event action: {event['action']}")


class DummyPlatformService(PlatformService):
    def __init__(self):
        super().__init__(
            owner='',
            repo_name='',
        )

    async def set_title(self, title: str):
        pass

    async def get_issues(self, state: str = "open") -> list[Issue]:
        return []

    async def publish_comment(self, text: str, issue_number: int) -> Optional[str]:
        return None

    async def update_comment(self, comment_id: str, body: str):
        pass

    async def find_existing_pr(self, head_branch: str, base_branch: str) -> Optional[int]:
        return None

    async def create_pr(
        self,
        title: str,
        bodies: list[str],
        draft: bool,
        head_branch: str,
        base_branch: str
    ) -> tuple[Optional[int], list[Union[str, Type[PlatformService.PRBodySentinel]]]]:
        return 1, [PlatformService.PRBodySentinel]

    async def update_pr_title(self, pr_number: int, title: str):
        pass

    async def update_pr_body(self, pr_number: int, body: str):
        pass
