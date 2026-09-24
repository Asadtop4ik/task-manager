from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from agent_deploy_monitor import ExternalDeployMonitor, _record

RUN_ID = "00000000-0000-0000-0000-000000000017"
SHA = "a" * 40


class FakeResponse:
    def __init__(self, value):
        self.raw = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        return self.raw[:size]


def pending(status="pr_ready", *, notified=True):
    return {
        "run_id": RUN_ID,
        "repo_full_name": "muradjanov-dev/ketoshop",
        "base_branch": "master",
        "pr_url": "https://github.com/muradjanov-dev/ketoshop/pull/7",
        "status": status,
        "merged_sha": SHA if status == "merged" else None,
        "notified": notified,
    }


class ExternalDeployMonitorTests(unittest.TestCase):
    def test_unknown_repo_or_branch_is_rejected(self):
        for changed in (
            {"repo_full_name": "other/repo"},
            {"base_branch": "main"},
            {"pr_url": "https://github.com/other/repo/pull/7"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                _record(pending() | changed)

    def test_unnotified_pr_waits_so_bot_can_send_its_link_first(self):
        requests = []

        def opener(request, timeout):
            requests.append(request.full_url)
            return FakeResponse([pending(notified=False)])

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.run_once(), (0, 0))
        self.assertEqual(len(requests), 1)

    def test_merged_pr_is_reported_without_claiming_deployment(self):
        requests = []

        def opener(request, timeout):
            requests.append((request.full_url, request.data))
            if request.full_url.endswith("/external-pending"):
                return FakeResponse([pending()])
            if request.full_url.endswith("/pulls/7"):
                return FakeResponse({"merged": True, "merge_commit_sha": SHA})
            if request.full_url.endswith("/merged"):
                self.assertEqual(json.loads(request.data), {"sha": SHA})
                return FakeResponse({"status": "merged"})
            raise AssertionError(request.full_url)

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github", opener=opener
        )
        self.assertEqual(monitor.run_once(), (1, 0))
        self.assertFalse(any("/deployed" in url for url, _ in requests))

    def test_exact_image_and_successful_workflow_are_both_required(self):
        for image_sha, expected in (("b" * 40, (0, 0)), (SHA, (0, 1))):
            with self.subTest(image_sha=image_sha):
                requests = []

                def opener(request, timeout):
                    requests.append((request.full_url, request.data))
                    if request.full_url.endswith("/external-pending"):
                        return FakeResponse([pending("merged")])
                    if "/workflows/deploy.yml/runs?" in request.full_url:
                        return FakeResponse(
                            {"workflow_runs": [{
                                "id": 123,
                                "head_sha": SHA,
                                "head_branch": "master",
                                "event": "push",
                                "status": "completed",
                                "conclusion": "success",
                            }]}
                        )
                    if request.full_url.endswith("/deployed"):
                        self.assertEqual(json.loads(request.data)["sha"], SHA)
                        return FakeResponse({"status": "deployed"})
                    raise AssertionError(request.full_url)

                def docker(args, **kwargs):
                    self.assertEqual(args[-1], "ketoshop")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "Config": {"Image": f"ghcr.io/muradjanov-dev/ketoshop:{image_sha}"},
                            "State": {"Running": True, "Health": {"Status": "healthy"}},
                        }),
                    )

                monitor = ExternalDeployMonitor(
                    callback_token="callback", github_token="github",
                    opener=opener, command_runner=docker,
                )
                self.assertEqual(monitor.run_once(), expected)
                self.assertEqual(
                    any(url.endswith("/deployed") for url, _ in requests),
                    expected == (0, 1),
                )

    def test_all_containers_must_match_the_same_merge_sha(self):
        images = {
            "qurbot-web": f"ghcr.io/muradjanov-dev/qurbot:{SHA}",
            "qurbot-worker": f"ghcr.io/muradjanov-dev/qurbot:{'b' * 40}",
        }

        def docker(args, **kwargs):
            container = args[-1]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "Config": {"Image": images[container]},
                    "State": {"Running": True, "Health": {"Status": "healthy"}},
                }),
            )

        monitor = ExternalDeployMonitor(
            callback_token="callback", github_token="github",
            opener=lambda *args, **kwargs: FakeResponse({}),
            command_runner=docker,
        )
        from agent_deploy_monitor import TARGETS

        self.assertFalse(monitor._production_matches(TARGETS["muradjanov-dev/qurbot"], SHA))


if __name__ == "__main__":
    unittest.main()
