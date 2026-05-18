"""TikTok Commercial Content API — quirk-handling helpers.

Centralizes adapter logic for known TikTok API oddities so every reader/
writer in the pipeline can call one function instead of each one
re-implementing the workaround (and silently getting it wrong, as we
discovered today across discover_tiktok_ads, discover_content_keywords,
refresh_ad_statuses, fetch_concat_match_ads, recover_lost_data,
rescreen_candidates, save_known_tiktok_ads, tiktok_tier2_fetch).

Known quirks documented here:

  1. **Numeric business_name** — The /ad/query/ and /ad/detail/ endpoints
     return the funder's numeric ID in `advertiser.business_name` (and
     usually duplicated in `advertiser.paid_for_by`) instead of the
     readable @handle for certain advertisers. We originally diagnosed
     this as a "deleted account" indicator, but @cmountouckos and
     @marioshaperis are LIVE accounts that exhibit the same behavior —
     so it's actually a "this advertiser routes payment through a
     separate funder entity" signal, not a deletion signal.

     Workaround: detect numeric `business_name`, fall back to a known-
     good handle from another source (the /advertiser/query/ endpoint,
     or our own cached candidates.csv lookup, or the caller's context).

  2. **Funder echo in paid_for_by** — When (1) happens, `paid_for_by`
     usually contains the SAME numeric ID. We don't trust paid_for_by
     unless it differs from business_name (when it does, it might be a
     real disclosed funder name).

These helpers do not make any API calls — they only normalize the data
the caller has already fetched. All API I/O still lives in
discover_tiktok_ads.py.
"""
from __future__ import annotations


def resolve_disclosed_name(advertiser_dict: dict | None,
                            fallback: str | None = None) -> str:
    """Return the best-known readable @handle for an advertiser, given
    the raw `advertiser` block from a TikTok API response.

    Args:
      advertiser_dict: the `advertiser` sub-dict from an /ad/query/ or
        /ad/detail/ item. May be None or empty.
      fallback: a handle the caller already knows (e.g. from a previous
        /advertiser/query/ call or from candidates.csv). Used if
        `business_name` is missing or numeric. Pass `str(business_id)`
        as the absolute-last-resort fallback so we never write None.

    Returns the best disclosed name (readable handle preferred,
    business_id as last resort). Never returns None — empty input
    + empty fallback returns ''.

    Examples:
      >>> resolve_disclosed_name({'business_name': 'cmountouckos'})
      'cmountouckos'
      >>> resolve_disclosed_name({'business_name': '7514704009685368854'}, fallback='cmountouckos')
      'cmountouckos'
      >>> resolve_disclosed_name({'business_name': ''}, fallback='nakiskyriakou')
      'nakiskyriakou'
      >>> resolve_disclosed_name(None, fallback='')
      ''
    """
    if not advertiser_dict:
        return fallback or ''
    name = (advertiser_dict.get('business_name') or '').strip()
    if name and not name.isdigit():
        return name
    return (fallback or '').strip()


def resolve_funded_by(advertiser_dict: dict | None) -> str | None:
    """Return the `paid_for_by` value, but only if it's distinct from
    the numeric business_name (i.e. likely a real disclosed funder).

    When TikTok hits the numeric-business_name quirk, paid_for_by
    usually echoes the SAME numeric ID — which is just the funder's
    business ID, not a meaningful disclosed funder name. In that case
    we return None so downstream code doesn't write the meaningless
    duplicate to the DB.
    """
    if not advertiser_dict:
        return None
    pby = (advertiser_dict.get('paid_for_by') or '').strip()
    if not pby:
        return None
    bn = (advertiser_dict.get('business_name') or '').strip()
    # If paid_for_by echoes the same numeric ID as business_name, it's noise.
    if pby.isdigit() and pby == bn:
        return None
    return pby


def is_numeric_handle_quirk(advertiser_dict: dict | None) -> bool:
    """True if this response shows the numeric-business_name quirk.
    Useful for logging / metrics so we can detect if it spreads to
    more advertisers over time.
    """
    if not advertiser_dict:
        return False
    name = (advertiser_dict.get('business_name') or '').strip()
    return bool(name) and name.isdigit()
