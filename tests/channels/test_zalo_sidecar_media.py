"""What the Zalo sidecar fetches for an inbound photo, and what it says when it can't.

The bug behind this file: a photo sent from the Zalo PC app answered 404 on the
Kubernetes deployment while a photo sent from the phone, on the same session
seconds later, downloaded fine — and because the sidecar made one bare
``fetch(content.href)`` with no headers, no retry and no fallback, and a
caption-less photo whose download fails carries no text either, the message was
dropped and the agent never learned a photo had been posted.

``app/channels/sidecars/zalo/media.js`` holds the answer to all of that, with
``fetch``/``sleep``/clock injected precisely so it can be driven from here.
There is no JS test harness in this repo, so each test runs one ``node``
subprocess: a driver imports the module, scripts the responses per URL, and
prints one JSON line. No network, no zca-js, no sidecar boot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.channels.sidecars.bootstrap import discover_sidecars

NODE = shutil.which("node")

# On a developer's box node may simply be absent; in CI it never is, and a
# silently skipped check reads exactly like a passing one.
pytestmark = pytest.mark.skipif(
    NODE is None and not os.environ.get("CI"),
    reason="node is not on PATH",
)

MEDIA_JS = next(p for p in discover_sidecars() if p.name == "zalo") / "media.js"

# stdin: the scenario. stdout: one JSON line. `responses` maps a URL to the
# answers it gives in order (the last one repeats), so a test asserts on the
# exact request sequence rather than on a global counter.
_DRIVER = r"""
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.env.MEDIA_JS).href);
let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const scenario = JSON.parse(raw);
const calls = [];
const sleeps = [];
let clock = 0;
const queues = new Map(Object.entries(scenario.responses || {}));
const fetchImpl = async (url, init) => {
  calls.push({
    url,
    headers: (init && init.headers) || null,
    hasSignal: Boolean(init && init.signal),
  });
  const queue = queues.get(url) || [{ status: 404 }];
  const step = queue.length > 1 ? queue.shift() : queue[0];
  clock += step.elapsed || 0;
  if (step.throw) {
    const err = new Error(step.throw);
    err.name = step.name || 'TypeError';
    throw err;
  }
  const headers = new Map(
    Object.entries(step.headers || {}).map(([k, v]) => [k.toLowerCase(), String(v)]),
  );
  const body = step.body === undefined ? 'jpegdata' : step.body;
  return {
    ok: step.status >= 200 && step.status < 300,
    status: step.status,
    headers: { get: (k) => {
      const key = String(k).toLowerCase();
      return headers.has(key) ? headers.get(key) : null;
    } },
    arrayBuffer: async () => {
      if (step.bodyThrow) throw new Error(step.bodyThrow);
      return Buffer.from(body);
    },
  };
};
const media = mod.mediaFromMessage(scenario.data, (line) => { ignored.push(line); });
const result = media ? await mod.downloadMedia(media, {
  maxBytes: scenario.maxBytes === undefined ? 1e9 : scenario.maxBytes,
  headersFor: async () => ({
    Accept: 'image/*',
    Origin: 'https://chat.zalo.me',
    Referer: 'https://chat.zalo.me/',
    'User-Agent': 'ZaloUA/1.0',
  }),
  fetchImpl,
  sleep: async (ms) => { sleeps.push(ms); clock += ms; },
  now: () => clock,
  attemptTimeoutMs: 50,
  ...(scenario.opts || {}),
}) : null;
console.log(JSON.stringify({
  media: media && {
    name: media.name, isFile: media.isFile, kind: media.kind,
    msgType: media.msgType, platformType: media.platformType,
    candidates: media.candidates, present: media.present, paramKeys: media.paramKeys,
  },
  result: result && { ...result, buffer: undefined, size: result.buffer ? result.buffer.length : 0 },
  calls,
  sleeps,
  ignored,
  notice: media && result && !result.ok ? mod.mediaFailureNotice(media, result) : null,
  thumbNotice: media ? mod.thumbnailNotice(media) : null,
}));
"""
_DRIVER = "const ignored = [];\n" + _DRIVER


def _run(scenario: dict) -> dict:
    proc = subprocess.run(
        [NODE or "node", "--input-type=module", "-e", _DRIVER],
        input=json.dumps(scenario),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "MEDIA_JS": str(MEDIA_JS)},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_ORIGINAL = "https://f37-zpg-r.zdn.vn/pics/original.jpg"
_HD = "https://f1-zpg-r.zdn.vn/pics/hd.jpg"
_THUMB = "https://f37-zpg-r.zdn.vn/pics/thumb.jpg"


def _photo(**overrides) -> dict:
    data = {
        "msgType": "chat.photo",
        "ts": "1000",
        "paramsExt": {"platformType": 2},
        "content": {
            "title": "",
            "href": _ORIGINAL,
            "thumb": _THUMB,
            "params": json.dumps({
                "hd": _HD, "thumbUrl": _THUMB, "normalUrl": _ORIGINAL, "jcp": "{}",
            }),
        },
    }
    data.update(overrides)
    return data


def test_the_working_path_still_makes_one_bare_request():
    """The request that succeeds today must not change shape.

    Headers are the retry's whole point, so the first attempt sends none: a CDN
    that is happy with the current request sees exactly the current request.
    """
    out = _run({"data": _photo(), "responses": {_ORIGINAL: [{"status": 200, "body": "jpeg"}]}})

    assert out["result"]["ok"] is True
    assert out["result"]["source"] == "href"
    assert out["result"]["size"] == 4
    assert out["sleeps"] == []
    assert [c["url"] for c in out["calls"]] == [_ORIGINAL]
    assert out["calls"][0]["headers"] is None
    # The abort signal is invisible to a server, so it rides even this attempt:
    # before it, a hung host stalled the message forever.
    assert out["calls"][0]["hasSignal"] is True


def test_candidates_run_href_then_params_then_thumb():
    out = _run({"data": _photo(), "responses": {_ORIGINAL: [{"status": 200}]}})

    assert [c["source"] for c in out["media"]["candidates"]] == [
        "href", "params.hd", "thumb",
    ]
    # normalUrl repeats the href: reported as offered, tried only once.
    assert out["media"]["present"] == ["href", "params.hd", "params.normalUrl", "thumb"]
    assert out["media"]["paramKeys"] == ["hd", "thumbUrl", "normalUrl", "jcp"]


def test_a_document_never_falls_back_to_its_thumbnail():
    """A thumb is a preview for a file, not the payload.

    Spooling it would hand the agent a JPEG called ``report.pdf``.
    """
    data = _photo(msgType="share.file")
    data["content"]["title"] = "report.pdf"
    out = _run({"data": data, "responses": {_ORIGINAL: [{"status": 404}]}})

    assert out["media"]["name"] == "report.pdf"
    assert out["media"]["isFile"] is True
    assert "thumb" not in [c["source"] for c in out["media"]["candidates"]]
    assert _THUMB not in [c["url"] for c in out["calls"]]


def test_the_primary_is_retried_with_backoff_before_a_fallback_is_tried():
    out = _run({
        "data": _photo(),
        "responses": {_ORIGINAL: [{"status": 404}], _HD: [{"status": 200, "body": "hd"}]},
    })

    assert out["result"]["ok"] is True
    assert out["result"]["source"] == "params.hd"
    assert [c["url"] for c in out["calls"]] == [_ORIGINAL, _ORIGINAL, _ORIGINAL, _HD]
    assert out["sleeps"] == [1000, 3000]


def test_every_attempt_after_the_first_carries_browser_headers():
    """What zca-js sends itself — and never the session cookie."""
    out = _run({
        "data": _photo(),
        "responses": {_ORIGINAL: [{"status": 404}, {"status": 200, "body": "ok"}]},
    })

    assert out["result"]["ok"] is True
    assert out["calls"][0]["headers"] is None
    retried = out["calls"][1]["headers"]
    assert retried["Referer"] == "https://chat.zalo.me/"
    assert retried["Origin"] == "https://chat.zalo.me"
    assert retried["User-Agent"] == "ZaloUA/1.0"
    assert "Cookie" not in retried


def test_a_definitive_status_is_not_retried():
    out = _run({
        "data": _photo(),
        "responses": {_ORIGINAL: [{"status": 403}], _HD: [{"status": 200, "body": "hd"}]},
    })

    assert out["result"]["source"] == "params.hd"
    assert [c["url"] for c in out["calls"]] == [_ORIGINAL, _HD]
    assert out["sleeps"] == []


def test_a_network_error_is_retried():
    out = _run({
        "data": _photo(),
        "responses": {_ORIGINAL: [{"throw": "fetch failed"}, {"status": 200, "body": "ok"}]},
    })

    assert out["result"]["ok"] is True
    assert len(out["calls"]) == 2
    assert out["result"]["attempts"][0]["status"].startswith("error:")


def test_a_body_that_dies_mid_read_is_a_failed_attempt_not_a_lost_message():
    """It used to escape to onMessage's catch, which dropped the message."""
    out = _run({
        "data": _photo(),
        "responses": {
            _ORIGINAL: [{"status": 200, "bodyThrow": "aborted"}, {"status": 200, "body": "ok"}],
        },
    })

    assert out["result"]["ok"] is True
    assert out["result"]["attempts"][0]["status"].startswith("error:body:")


def test_an_empty_body_moves_on_without_retrying():
    out = _run({
        "data": _photo(),
        "responses": {
            _ORIGINAL: [{"status": 200, "body": ""}], _HD: [{"status": 200, "body": "hd"}],
        },
    })

    assert out["result"]["source"] == "params.hd"
    assert [c["url"] for c in out["calls"]] == [_ORIGINAL, _HD]


def test_a_thumbnail_success_is_reported_as_degraded():
    out = _run({
        "data": _photo(),
        "responses": {
            _ORIGINAL: [{"status": 404}], _HD: [{"status": 404}],
            _THUMB: [{"status": 200, "body": "small"}],
        },
    })

    assert out["result"]["ok"] is True
    assert out["result"]["source"] == "thumb"
    assert out["result"]["degraded"] is True
    assert out["thumbNotice"] == (
        "[the photo above is a low-resolution thumbnail; the full copy could "
        "not be downloaded]"
    )


def test_over_cap_media_ends_the_ladder_without_trying_a_smaller_copy():
    out = _run({
        "data": _photo(),
        "maxBytes": 10,
        "responses": {_ORIGINAL: [{"status": 200, "headers": {"content-length": "999"}}]},
    })

    assert out["result"]["ok"] is False
    assert out["result"]["capped"] is True
    assert len(out["calls"]) == 1


def test_everything_failing_reports_the_status_hosts_and_offered_fields():
    """The line that has to pin the cause the next time this happens."""
    out = _run({"data": _photo(), "responses": {}})

    result = out["result"]
    assert result["ok"] is False
    assert result["status"] == 404
    assert result["described"] == "f37-zpg-r.zdn.vn/pics/original.jpg"
    assert result["urls"] == [
        "f37-zpg-r.zdn.vn/pics/original.jpg",
        "f1-zpg-r.zdn.vn/pics/hd.jpg",
        "f37-zpg-r.zdn.vn/pics/thumb.jpg",
    ]
    assert [f"{a['source']}:{a['status']}" for a in result["attempts"]] == [
        "href:404", "href:404", "href:404", "params.hd:404", "thumb:404",
    ]
    assert result["present"] == ["href", "params.hd", "params.normalUrl", "thumb"]
    assert result["paramKeys"] == ["hd", "thumbUrl", "normalUrl", "jcp"]


def test_a_signed_url_keeps_its_signature_out_of_the_log():
    data = _photo()
    data["content"]["href"] = "https://f37-zpg-r.zdn.vn/pics/x.jpg?token=SECRET&e=1"
    data["content"]["params"] = "{}"
    out = _run({"data": data, "responses": {}})

    described = out["result"]["described"]
    assert described == "f37-zpg-r.zdn.vn/pics/x.jpg?token&e"
    assert "SECRET" not in json.dumps(out["result"])


def test_the_budget_stops_the_ladder():
    out = _run({
        "data": _photo(),
        "opts": {"totalBudgetMs": 1000},
        "responses": {},
    })

    assert out["result"]["budgetSpent"] is True
    assert len(out["calls"]) == 2


def test_unparseable_params_are_reported_rather_than_guessed_at():
    data = _photo()
    data["content"]["params"] = "{not json"
    out = _run({"data": data, "responses": {}})

    assert out["media"]["paramKeys"] == ["<unparseable>"]
    assert [c["source"] for c in out["media"]["candidates"]] == ["href", "thumb"]


def test_the_notice_names_a_file_but_never_a_photos_cdn_basename():
    """A photo's basename is an md5 and two ids — noise to the agent."""
    photo = _run({"data": _photo(), "responses": {}})
    assert photo["notice"] == "[sent a photo, but it could not be downloaded (HTTP 404)]"
    assert "original.jpg" not in photo["notice"]

    data = _photo(msgType="share.file")
    data["content"]["title"] = "report.pdf"
    doc = _run({"data": data, "responses": {}})
    assert doc["notice"] == (
        "[sent a file: report.pdf, but it could not be downloaded (HTTP 404)]"
    )


def test_a_network_failure_reads_as_words_not_a_status_code():
    out = _run({
        "data": _photo(),
        "responses": {url: [{"throw": "fetch failed"}] for url in (_ORIGINAL, _HD, _THUMB)},
    })

    assert out["notice"] == "[sent a photo, but it could not be downloaded (fetch failed)]"


def test_a_sticker_is_not_media_and_is_logged_as_ignored():
    data = _photo(msgType="chat.sticker")
    out = _run({"data": data, "responses": {}})

    assert out["media"] is None
    assert out["ignored"] == ["media-like content ignored (msgType=chat.sticker)"]
