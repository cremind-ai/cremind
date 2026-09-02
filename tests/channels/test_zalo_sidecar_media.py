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
    // undici hides the reason that names a network failure in `cause`.
    if (step.cause) err.cause = new Error(step.cause);
    throw err;
  }
  const headers = new Map(
    Object.entries(step.headers || {}).map(([k, v]) => [k.toLowerCase(), String(v)]),
  );
  const body = step.body === undefined ? 'jpegdata' : step.body;
  return {
    ok: step.status >= 200 && step.status < 300,
    status: step.status,
    // `url` is where a redirect LANDED. Spread conditionally so a step that
    // does not set one leaves the property absent, exactly like the synthetic
    // Response the fallback in fetchOne exists for.
    ...(step.url === undefined ? {} : { url: step.url }),
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
  trail: result ? mod.attemptTrail(result.attempts) : null,
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


# A file rides a different CDN from a photo: its href redirects to a "router"
# that picks an edge from the CLIENT's network, so a server on a hosting
# provider is answered 404 while a home connection is redirected to an edge.
_FILE_HREF = "https://file-stal-22.dlfl.vn/gr/b8b446fc/2aOboQvoizAT2bgyUIVtgwSd"
_ROUTER = "https://file-stal-22.flchat.vn/gr/b8b446fc/2aOboQvoizAT2bgyUIVtgwSd"
_EDGE_SUFFIXES = [
    "te-vnso-ne-2", "te-vnso-ne-1", "te-vnso-pt-64", "te-vnso-pt-65",
    "te-vnso-ne-3", "te-vnso-pt-63",
]


def _edge(suffix: str) -> str:
    return f"https://file-stal-22-{suffix}.flchat.vn/gr/b8b446fc/2aOboQvoizAT2bgyUIVtgwSd"


def _file(**overrides) -> dict:
    """A share.file exactly as the failing PC-app message came in: one href,
    and params that carry checksums but no second copy to fall back to."""
    data = {
        "msgType": "share.file",
        "ts": "1000",
        "paramsExt": {"platformType": 2},
        "content": {
            "title": "report.pdf",
            "href": _FILE_HREF,
            "thumb": _THUMB,
            "params": json.dumps({
                "fileSize": "175067", "checksum": "abc", "checksumSha": "",
                "fileExt": "pdf", "fdata": "{}", "fType": 1,
            }),
        },
    }
    data.update(overrides)
    return data


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


# --- the file CDN's router ---------------------------------------------------
#
# A file's href redirects to `file-<pool>-<n>.flchat.vn`, which picks a CDN edge
# from the CLIENT's network: a residential ISP is redirected on to an edge, a
# hosting network is answered a bare 404 — same object, same second, headers
# irrelevant. The edges serve that path to anyone who asks them by name, so a
# router 404 is answered by asking them directly rather than by giving up.


def _router_404(landed: str = _ROUTER) -> dict:
    """What a hosted server gets: the redirect happened, the router refused."""
    return {"status": 404, "url": landed}


def test_a_router_404_goes_straight_to_the_cdn_edges():
    out = _run({
        "data": _file(),
        "responses": {
            _FILE_HREF: [_router_404()],
            _edge("te-vnso-ne-2"): [{"status": 200, "body": "pdfbytes"}],
        },
    })

    assert out["result"]["ok"] is True
    assert out["result"]["source"] == "edge.te-vnso-ne-2"
    assert out["result"]["size"] == 8
    # The router's answer is about our network, not this moment, so asking it
    # again is waiting for nothing — that backoff is what the first failing
    # deployment spent before reporting a file nobody could see.
    assert out["sleeps"] == []
    assert [c["url"] for c in out["calls"]] == [_FILE_HREF, _edge("te-vnso-ne-2")]
    # The log line now says who answered, which the first one could not.
    assert out["trail"] == (
        "href:404@file-stal-22.flchat.vn,edge.te-vnso-ne-2:200"
    )


def test_the_first_request_is_still_bare_and_the_edges_are_not():
    out = _run({
        "data": _file(),
        "responses": {
            _FILE_HREF: [_router_404()],
            _edge("te-vnso-ne-2"): [{"status": 200}],
        },
    })

    assert out["calls"][0]["headers"] is None
    assert out["calls"][1]["headers"]["Referer"] == "https://chat.zalo.me/"


def test_the_edges_are_walked_in_measured_order_until_one_answers():
    """Two of the six answered 404 for an object the others served, so a single
    fallback name would have fixed nothing."""
    out = _run({
        "data": _file(),
        "responses": {
            _FILE_HREF: [_router_404()],
            _edge("te-vnso-ne-2"): [{"status": 404}],
            _edge("te-vnso-ne-1"): [{"status": 403}],
            _edge("te-vnso-pt-64"): [{"throw": "fetch failed"}],
            _edge("te-vnso-pt-65"): [{"status": 200, "body": "pdf"}],
        },
    })

    assert out["result"]["source"] == "edge.te-vnso-pt-65"
    assert [c["url"] for c in out["calls"]] == [
        _FILE_HREF,
        _edge("te-vnso-ne-2"), _edge("te-vnso-ne-1"),
        _edge("te-vnso-pt-64"), _edge("te-vnso-pt-65"),
    ]
    # An edge is an alternate: one try each, no backoff between them.
    assert out["sleeps"] == []


def test_every_edge_failing_still_tells_the_sender_the_http_status():
    """A drifted CDN name must not leak into the chat as the failure reason."""
    out = _run({
        "data": _file(),
        "responses": {
            _FILE_HREF: [_router_404()],
            **{
                _edge(s): [{
                    "throw": "fetch failed",
                    "cause": f"getaddrinfo ENOTFOUND file-stal-22-{s}.flchat.vn",
                }]
                for s in _EDGE_SUFFIXES
            },
        },
    })

    assert out["result"]["ok"] is False
    # Six DNS errors after one real 404: the 404 is the answer that means
    # something to the person who sent the file.
    assert out["result"]["status"] == 404
    assert out["notice"] == (
        "[sent a file: report.pdf, but it could not be downloaded (HTTP 404)]"
    )
    # ...while the log keeps the reason undici buried in `cause`.
    assert "getaddrinfo ENOTFOUND" in out["trail"]
    assert len(out["result"]["urls"]) == 1 + len(_EDGE_SUFFIXES)


def test_a_file_whose_edges_all_404_reports_the_router_and_every_edge():
    out = _run({
        "data": _file(),
        "responses": {_FILE_HREF: [_router_404()]},
    })

    assert out["result"]["status"] == 404
    assert out["trail"] == ",".join(
        ["href:404@file-stal-22.flchat.vn"]
        + [f"edge.{s}:404" for s in _EDGE_SUFFIXES]
    )
    # A share.file offers no second copy and never falls back to a thumbnail,
    # so the edges are the whole ladder after the href.
    assert out["media"]["present"] == ["href"]


def test_the_edge_host_comes_from_where_the_redirect_landed():
    """The href is on dlfl.vn, which has no edges at all; the pool label has to
    be read off the router the redirect actually reached."""
    out = _run({
        "data": _file(),
        "responses": {
            _FILE_HREF: [{"status": 404, "url": _ROUTER.replace("22", "7")}],
            _edge("te-vnso-ne-2").replace("22", "7"): [{"status": 200}],
        },
    })

    assert out["result"]["source"] == "edge.te-vnso-ne-2"
    assert out["calls"][1]["url"] == _edge("te-vnso-ne-2").replace("22", "7")


def test_an_href_that_is_already_the_router_needs_no_redirect_to_trigger():
    """A Response with no url at all — the fallback path in fetchOne."""
    data = _file()
    data["content"]["href"] = _ROUTER
    out = _run({
        "data": data,
        "responses": {
            _ROUTER: [{"status": 404}],
            _edge("te-vnso-ne-2"): [{"status": 200}],
        },
    })

    assert out["result"]["source"] == "edge.te-vnso-ne-2"
    # Nothing moved, so the trail carries no host token.
    assert out["trail"] == "href:404,edge.te-vnso-ne-2:200"


@pytest.mark.parametrize("landed", [
    None,                                        # no redirect: the href itself
    "https://f37-zpg-r.zdn.vn/pics/original.jpg",  # the photo CDN
    "https://res-zalo-1.zdn.vn/pics/x.jpg",      # same shape, different CDN
    _edge("te-vnso-ne-2"),                       # an edge is not a router
])
def test_a_404_from_anywhere_but_the_router_keeps_todays_retries(landed):
    """A match sends six extra requests, so the pattern must match the router
    and nothing else."""
    step = {"status": 404} if landed is None else {"status": 404, "url": landed}
    out = _run({"data": _photo(), "responses": {_ORIGINAL: [step]}})

    assert [c["url"] for c in out["calls"]][:3] == [_ORIGINAL] * 3
    assert out["sleeps"] == [1000, 3000]
    assert not any("flchat.vn" in c["url"] for c in out["calls"])


def test_the_edges_are_synthesised_once_per_download():
    """Every copy a message names sits on one pool; a second router 404 from a
    params.* copy must not queue the same six again."""
    data = _photo()
    data["content"]["params"] = json.dumps({"hd": _ROUTER + "-hd"})
    out = _run({
        "data": data,
        "responses": {
            _ORIGINAL: [{"status": 404, "url": _ROUTER}],
            _ROUTER + "-hd": [{"status": 404, "url": _ROUTER + "-hd"}],
        },
    })

    edge_calls = [c["url"] for c in out["calls"] if "-te-vnso-" in c["url"]]
    assert len(edge_calls) == len(_EDGE_SUFFIXES)


def test_the_walk_can_be_pointed_somewhere_else_or_turned_off():
    off = _run({
        "data": _file(),
        "opts": {"edgeSuffixes": []},
        "responses": {_FILE_HREF: [_router_404()]},
    })
    assert [c["url"] for c in off["calls"]] == [_FILE_HREF] * 3
    assert off["sleeps"] == [1000, 3000]

    custom = _run({
        "data": _file(),
        "opts": {"edgeSuffixes": ["te-vnso-pt-64", "te-vnso-ne-2"]},
        "responses": {
            _FILE_HREF: [_router_404()],
            _edge("te-vnso-ne-2"): [{"status": 200}],
        },
    })
    assert [c["url"] for c in custom["calls"]] == [
        _FILE_HREF, _edge("te-vnso-pt-64"), _edge("te-vnso-ne-2"),
    ]


def test_a_signed_url_reaches_the_edge_intact_but_never_the_log():
    signed = f"{_ROUTER}?token=SECRET&e=1"
    out = _run({
        "data": _file(),
        "responses": {
            _FILE_HREF: [{"status": 404, "url": signed}],
            f"{_edge('te-vnso-ne-2')}?token=SECRET&e=1": [{"status": 200}],
        },
    })

    assert out["result"]["ok"] is True
    assert "SECRET" not in json.dumps(out["result"])
    assert out["result"]["urls"][1].endswith("?token&e")


def test_the_budget_stops_the_edge_walk_too():
    """Six extra hosts must not become six extra ways to stall a message: the
    walk starts attempts only while the budget lasts (slow answers here, since
    the edges are asked back-to-back with nothing to sleep on)."""
    out = _run({
        "data": _file(),
        "opts": {"totalBudgetMs": 1000},
        "responses": {
            _FILE_HREF: [{"status": 404, "url": _ROUTER, "elapsed": 600}],
            _edge("te-vnso-ne-2"): [{"status": 404, "elapsed": 600}],
        },
    })

    assert out["result"]["budgetSpent"] is True
    assert len(out["calls"]) == 2


def test_an_over_cap_edge_ends_the_ladder():
    out = _run({
        "data": _file(),
        "maxBytes": 10,
        "responses": {
            _FILE_HREF: [_router_404()],
            _edge("te-vnso-ne-2"): [
                {"status": 200, "headers": {"content-length": "999"}},
            ],
        },
    })

    assert out["result"]["capped"] is True
    assert len(out["calls"]) == 2
