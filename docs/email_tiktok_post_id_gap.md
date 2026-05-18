# Email — TikTok ad library is missing post URLs

Below are three drafts at different levels of formality. Pick the one that
matches your recipient.

---

## DRAFT 1 — To TikTok Policy / Trust & Safety / DSA contact

> **Subject:** Unable to report Cyprus political ads — TikTok Ad Library strips the original post identifier

Dear TikTok Trust & Safety / Public Policy team,

I'm writing on behalf of an independent monitoring project tracking political
advertising on TikTok in the run-up to the **Cyprus 2026 parliamentary
elections**. As you know, TikTok's policy prohibits political advertising
globally; nonetheless we have identified **60+ candidates, 4 party-supporter
accounts and 2 party-account accounts** running boosted political content in
Cyprus, alongside many more advertisers still under verification.

I want to flag a structural limitation that prevents us from filing standard
violation reports through TikTok's normal reporting flow.

**The problem**

When the TikTok Research API (`/v2/research/adlib/ad/query/`) returns a
political-content ad, we receive a `business_id`, an `ad_id`, ad metadata, and
a link to the Ad Library detail page
(`https://library.tiktok.com/ads/detail/?ad_id=<id>`). What we do **not**
receive — and what is **deliberately omitted from the rendered HTML of the
Ad Library page itself** — is the canonical post URL on `tiktok.com`
(`https://www.tiktok.com/@<handle>/video/<post_id>` or `/photo/<post_id>`).

Because the post ID is stripped at the HTML level, no reverse lookup is
possible from `ad_id` → original post URL. The only automated workaround we
have today is scraping the advertiser's public profile feed and matching posts
to ads by date and creative hash — which is slow, brittle, and breaks any
time the user reorders or deletes posts.

**Why this matters**

TikTok's own **bulk URL reporter form** for trusted-flaggers and researchers
exposes four fields per row:

| Field | Required | Notes |
|---|---|---|
| URL to be reported | ✓ | Free-text URL input |
| Category of URL | ✓ | Dropdown |
| Report reason | ✓ | Dropdown |
| Additional details | — | Free text |

The "URL to be reported" field rejects `library.tiktok.com/ads/detail/?ad_id=…`
links — it requires a canonical `tiktok.com/@<handle>/(video|photo)/<post_id>`
URL. The same constraint applies to TikTok's in-app "Report" flow, the
European Commission's DSA transparency filings, and third-party platforms
like ELSA / GLOBSEC.

The result is a paradox: researchers can *find* policy-violating political
ads via the Research API, can *prove* they exist via the Ad Library page,
can quantify their reach — but **cannot submit them through any of the
reporting channels TikTok itself provides**, because the required input
field is the one piece of information TikTok refuses to expose.

Our project has prepared a CSV of 60+ confirmed Cyprus parliamentary
candidates and party-account advertisers, in the exact column shape your
bulk reporter expects (URL / Category / Reason / Additional details). The
only blocker is the URL column: we can fill it with Ad Library links, but
the form rejects them.

**What we'd like**

Either of the following would resolve this:

1. Expose `post_id` and the canonical `tiktok.com/@handle/video|photo/<id>`
   URL in the `/v2/research/adlib/ad/detail/` response, OR
2. Make TikTok's in-app reporting flow accept `library.tiktok.com/ads/detail/`
   URLs as a valid input.

I'd be happy to share our dataset (currently ~500 ads across 75 advertisers,
publicly viewable at https://cyprus-2026-tiktok.streamlit.app/) and discuss
how this gap is materially limiting election-integrity work in Cyprus.

Best regards,
[Your name]
[Your role / organisation]
[Email · phone]

---

## DRAFT 2 — To national regulator (e.g. Cyprus Commissioner of Communications, EPRA, ERGA)

> **Subject:** Technical barrier preventing Cyprus political-ad reports to TikTok ahead of 2026 elections

Dear [Commissioner / Director],

I am writing to flag a technical issue that is materially obstructing
independent monitoring of political advertising on TikTok in the run-up to the
Cyprus 2026 parliamentary elections.

Our project — which uses TikTok's official Research API to surface ads that
violate the platform's global ban on political advertising — has so far
documented **60+ confirmed parliamentary candidates and several party accounts
and supporter accounts** running paid political content. Our public dashboard
is at https://cyprus-2026-tiktok.streamlit.app/.

**The blocker**

TikTok's Ad Library API exposes each ad by an internal `ad_id`. It does **not**
expose the original `tiktok.com` post URL. The Ad Library detail page in a
web browser likewise withholds this identifier — it is deliberately stripped
from the rendered HTML — so no automated reverse-lookup is possible.

This creates a one-way mirror: we can see the ad, we know it exists, we know
which advertiser ran it, we can quantify its reach — but we cannot generate
the canonical `/@handle/video/<post_id>` URL that every existing
moderation/violation-reporting channel (including TikTok's own in-app
"Report" flow, DSA Article 26/40 filings, and most third-party
fact-checking platforms) requires as input.

The only workaround we have is to scrape the advertiser's public profile,
list every post, and try to match against our ads by date and visual hash.
This is fragile, slow, and rate-limited, and breaks whenever a user deletes
or reorders content.

**What we are asking**

We would value the regulator's support in raising this with TikTok directly —
either through bilateral channels, ERGA / EPRA cooperation, or as a finding in
the DSA Code of Practice on Disinformation reporting cycle. A single field
addition to the `/ad/detail/` Research API endpoint (`post_id` + canonical
post URL) would close the gap entirely.

I am happy to provide a full technical briefing and our full dataset.

Best regards,
[Your name]

---

## DRAFT 3 — Short / to a fact-checking partner

> **Subject:** Quick FYI — why we can't report the Cyprus TikTok political ads through normal channels

Hi [name],

Quick note on why our Cyprus political-ad dataset can't be funnelled into the
standard reporting pipelines:

TikTok's Research API gives us the `ad_id` and the Ad Library link
(`library.tiktok.com/ads/detail/?ad_id=...`) — but **not** the canonical
post URL (`tiktok.com/@user/video/<id>`). The post ID is stripped from the Ad
Library page HTML itself, so no reverse lookup. The only workaround is
scraping each candidate's profile and matching posts by date, which doesn't
scale.

Every reporting channel I know — TikTok's in-app Report, DSA filings, your
own pipeline — accepts only the canonical post URL. So we're stuck with
"here's a list of 60 candidates running ads, here are 500 Ad Library links,
but I can't hand you the URLs your form needs."

Two ways out:
1. Push TikTok (via DSA / Code of Practice) to expose `post_id` in the
   `/ad/detail/` Research endpoint.
2. Persuade reporting platforms to accept `library.tiktok.com/ads/detail/`
   links as a valid format.

If you have an existing channel to either of those, would love to compare
notes. Dataset is at https://cyprus-2026-tiktok.streamlit.app/ if useful.

Cheers,
[Your name]
