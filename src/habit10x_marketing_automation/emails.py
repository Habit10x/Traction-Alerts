"""Render one alert email (subject, HTML, plain text) from a fired rule."""

import html
import re
from collections import Counter
from typing import TYPE_CHECKING

from .config import Settings
from .store import RuleConfig

if TYPE_CHECKING:  # avoid a runtime import cycle with engine.py
    from .engine import Firing
    from .rules import RuleDef

ACCENT = "#7B1C1C"


def plain(text: object) -> str:
    """Challenge titles are rich HTML in the database: drop tags, decode entities, collapse spaces."""
    s = re.sub(r"<[^>]*>", " ", str(text or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def plural(n: int, unit: tuple[str, str]) -> str:
    return f"{n} {unit[0] if n == 1 else unit[1]}"


def _challenge(info: dict) -> str:
    num = info.get("num")
    title = plain(info.get("title"))
    return (f"Challenge #{num}" if num is not None else "A challenge") + (f' "{title}"' if title else "")


def entity_label(info: dict, entity: str) -> str:
    """Human label for a per-item entity (a challenge number, a link slug) instead of a raw id."""
    if info.get("num") is not None:
        return f"Challenge #{info['num']}"
    return str(info.get("slug") or entity or "")


def _who(info: dict) -> str:
    name = plain(info.get("user_name") or info.get("name")) or "Guest"
    return name + (" (guest)" if info.get("guest") else "")


def _source(info: dict) -> str:
    platform, slug = info.get("platform"), info.get("slug")
    return f"{platform} ({slug})" if platform and slug else (slug or platform or "unknown source")


def _line(rule_id: str, info: dict) -> str:
    if rule_id == "outside_attempt":
        score = f", score {float(info['wit_score']):g}" if info.get("wit_score") is not None else ""
        return f"{_challenge(info)} - {info.get('response_type')}{score} - {_who(info)}"
    if rule_id in ("new_signup", "source_signup"):
        return f"{plain(info.get('name')) or 'New user'} <{info.get('email')}> - from {_source(info)}" + (
            f" - {info['country']}" if info.get("country") else "")
    if rule_id == "plan_started":
        return f"{plain(info.get('name'))} <{info.get('email')}> - plan {info.get('plan')} ({info.get('status') or 'no status'})"
    if rule_id == "great_response":
        return f"Score {float(info['wit_score']):g} - {_challenge(info)} - {info.get('response_type')} by {_who(info)}"
    if rule_id == "pricing_interest":
        return f"{info.get('path')} - {info.get('country') or '?'} - {info.get('device') or 'unknown device'}"
    if rule_id == "system_error":
        return f"{info.get('source')}: {info.get('error')}"
    if rule_id == "social_traction":
        return (f"{info.get('platform')}: {info.get('title') or info.get('video_url')} - {int(info['value']):,} "
                f"{info['metric']} (threshold {int(info['threshold']):,}; was {int(info['before']):,} at the previous check)")
    return _challenge(info)


def _subject(rule: "RuleDef", cfg: RuleConfig, f: "Firing") -> str:
    n, info = f.count, f.events[0].info
    window = cfg.window_minutes
    match rule.id:
        case "outside_attempt":
            return (f"Outside visitor tried {_challenge(info)}" if len(f.events) == 1
                    else f"{plural(n, rule.unit)} by outside visitors in the last {window} min")
        case "new_signup":
            return (f"New signup: {plain(info.get('name')) or info.get('email')} from {_source(info)}"
                    if len(f.events) == 1 else f"{n} new signups in the last {window} min")
        case "plan_started":
            return (f"Plan started: {plain(info.get('name')) or info.get('email')} ({info.get('plan')})"
                    if len(f.events) == 1 else f"{n} plans started in the last {window} min")
        case "great_response":
            return (f"Great response (score {float(info['wit_score']):g}) on {_challenge(info)} - feature it?"
                    if len(f.events) == 1 else f"{n} great responses in the last {window} min - feature them?")
        case "source_visitors":
            return f"{plural(n, rule.unit)} from {_source(info)} in the last {window} min"
        case "source_signup":
            return (f"Signup from {_source(info)}: {plain(info.get('name')) or info.get('email')}"
                    if len(f.events) == 1 else f"{n} signups from social links in the last {window} min")
        case "challenge_visitors":
            return f"{_challenge(info)} is catching on: {plural(n, rule.unit)} in the last {window} min"
        case "attempts_surge":
            return f"{_challenge(info)}: {plural(n, rule.unit)} attempting in the last {window} min"
        case "pricing_interest":
            return f"Interested in paying: {plural(n, rule.unit)} on Upgrade in the last {window} min"
        case "site_visitors":
            return f"Site-wide surge: {plural(n, rule.unit)} in the last {window} min"
        case "system_error":
            return f"System cannot read {info.get('source')} data ({n} checks in a row)"
        case "social_traction":
            title = str(info.get("title") or info.get("video_url") or "")[:70]
            metrics = ", ".join(sorted({e.info["metric"] for e in f.events}))
            return (f"Video reached {int(info['threshold']):,} {info['metric']}: {title} ({info.get('platform')})"
                    if len(f.events) == 1 else f"{len(f.events)} video milestones ({metrics})")
    return f"{rule.name}: {n}"


def _link(rule: "RuleDef", f: "Firing", settings: Settings) -> tuple[str, str] | None:
    if f.events[0].info.get("video_url"):
        return "Open video", str(f.events[0].info["video_url"])
    num = f.events[0].info.get("num")
    if num is not None:
        return "Open challenge", f"{settings.site_url.rstrip('/')}/challenge/{num}"
    return None


def render_alert(rule: "RuleDef", cfg: RuleConfig, firing: "Firing", settings: Settings) -> tuple[str, str, str]:
    subject = _subject(rule, cfg, firing)
    if rule.id == "system_error":
        threshold_line = f"{firing.count} failed checks in a row (threshold {cfg.threshold})"
    elif rule.id == "social_traction":
        passed = ", ".join(sorted({f"{e.info['metric']} {int(e.info['threshold']):,}" for e in firing.events}))
        threshold_line = f"{plural(firing.count, rule.unit)} passed since the previous check ({passed})"
    else:
        threshold_line = (f"{plural(firing.count, rule.unit)} in the last {cfg.window_minutes} min "
                          f"(threshold: {cfg.threshold} or more)")

    if rule.id == "site_visitors":
        top = Counter(plain(e.info.get("path")) for e in firing.events).most_common(5)
        lines = [f"{path or '/'} ({n} views)" for path, n in top]
    else:
        lines = [_line(rule.id, e.info) for e in sorted(firing.events, key=lambda e: e.ts)[-8:]]
    if len(firing.events) > 8 and rule.id != "site_visitors":
        lines.append(f"...and {len(firing.events) - 8} earlier")

    link = _link(rule, firing, settings)
    footer = (f"Alert: {rule.name}. Change the threshold in the Traction dashboard"
              + (" (Social tracker page)." if rule.id == "social_traction" else "."))

    text = "\n".join([subject, "", f"Threshold reached: {threshold_line}", "", *[f"- {line}" for line in lines],
                      *(["", f"{link[0]}: {link[1]}"] if link else []), "", footer])
    items = "".join(f'<li style="margin:0 0 6px">{html.escape(line)}</li>' for line in lines)
    button = (f'<p style="margin:22px 0 0"><a href="{html.escape(link[1])}" style="background:{ACCENT};color:#fff;'
              f'padding:11px 18px;border-radius:8px;text-decoration:none;font-weight:600">{html.escape(link[0])}</a></p>'
              if link else "")
    body = f"""<!DOCTYPE html><html><body style="margin:0;background:#f9fafb;font-family:-apple-system,Segoe UI,Roboto,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:28px 12px"><tr><td align="center">
<table width="100%" style="max-width:520px;background:#fff;border-radius:14px;overflow:hidden">
<tr><td style="background:{ACCENT};padding:16px 24px;color:#fff;font-weight:700">Speak Sharp - Traction alert</td></tr>
<tr><td style="padding:26px 24px;color:#111827">
<p style="margin:0 0 6px;font-size:19px;font-weight:700">{html.escape(subject)}</p>
<p style="margin:0 0 18px;font-size:13px;color:#6b7280">Threshold reached: {html.escape(threshold_line)}</p>
<ul style="margin:0;padding-left:18px;font-size:14px;line-height:1.45">{items}</ul>{button}
<p style="margin:24px 0 0;font-size:12px;color:#9ca3af">{html.escape(footer)}</p>
</td></tr></table></td></tr></table></body></html>"""
    return subject, body, text
