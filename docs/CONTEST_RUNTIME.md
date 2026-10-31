# Competition runtime

`ACTION_MODE=contest` enables the official action adapter. `mock` and `off` remain available.

Private source documents, interview notes, API metadata, deployment bundles and credentials are excluded from Git. Build the private bundle with `python scripts/build_contest_bundle.py` after placing the acquired materials under `.local/contest/`.

Required server settings:

- `ACTION_MODE=contest`
- `LLM_MODE=openai`
- `WEB_SEARCH_MODE=off`
- `CONTEST_ACTION_TOKEN`: the participant's action token
- `CONTEST_BUNDLE_1` through `CONTEST_BUNDLE_16`: consecutive chunks of the base64/zlib bundle, or `CONTEST_BUNDLE_B64` for smaller bundles
- `DATA_DIR`: persistent writable disk

For local work, the default bundle path is `.local/contest/runtime-bundle.json`. It can be overridden with `CONTEST_BUNDLE_PATH`.

The adapter derives tools from the supplied OpenAPI specification, validates arguments, fixes the destination host, and supplies the TC header outside the model. Administrative/reset endpoints are never exposed. A labelled TC ID in the incoming message starts a separate persistent conversation; follow-up messages retain that ID. Never substitute a public practice ID for an unknown evaluation ID.

Identifiers must originate in customer messages or earlier scoped API results. Exact repeated writes use a persistent receipt journal. API request acceptance is reported separately from actual approval, payment or shipment completion. Rate limiting and retries are bounded; each turn has a response deadline below the judge's silence limit. Replies use one SimpleText bubble of at most 500 characters.

Knowledge is available through document lookup/search and selected policy context. Calendar and partial-refund arithmetic are deterministic tools. Images explicitly linked in messages are accepted only from the contest's Kakao CDN path; arbitrary external URLs are not fetched.

Before submission, run the unit suite, exercise representative public cases through Kakao, and confirm actual receipt IDs and conversation isolation. Reset only practice data when re-running stateful scenarios. Do not deploy, edit prompts, change settings or reset data after final submission.
