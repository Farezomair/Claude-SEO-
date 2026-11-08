"""The approved email templates. Approve these once; the agent only fills the {slots}.

Written for owners and admissions staff, not marketers: everyday words, no SEO jargon.
Rules baked in: plain text, no links in the cold emails apart from the company domain, no
attachments before they say yes, no em dashes, mailing address and opt-out line in
every cold email.
"""

FOOTER = """{sender_full}
{company} | {tagline}
{website}
{address}

If this isn't relevant, just reply "no" and I won't follow up."""

SHORT_FOOTER = """{company}, {address}
Reply "no" and I won't email again."""

# The offer paragraph rotates between approved versions so no two emails in a batch read the same.
OFFERS = [
    """I do local search for treatment centers. If it's useful, I'll send you a free one-page breakdown: what a family in {area} sees when they search for {service_phrase}, where {center} shows up, and the first three things I'd fix. By email within two days, no call needed. Want it?""",
    """I do local search for treatment centers. Happy to send a free one-page breakdown for {center}: who a family in {area} finds when they search for {service_phrase}, where you come up, and what I'd fix first. It comes by email within two days, no call needed. Should I send it?""",
    """I do local search for treatment centers, and I'd be glad to send {center} a free one-page breakdown: what comes up when someone in {area} searches for {service_phrase}, where you land, and the first three fixes I'd make. By email, no call needed. Want me to?""",
]

TEMPLATES = {
    "email1": {
        "subject": "{subject}",
        "body": """Hi {name},

{opening}

{offer}

""" + FOOTER,
    },
    "fu1": {
        "subject": "Re: {thread_subject}",
        "body": """Hi {name},

One more thought on {center}. {fu1_line}

The free one-page breakdown is still yours if you want it. Just reply "yes" and I'll put it together.

{sender_first}

""" + SHORT_FOOTER,
    },
    "fu2": {
        "subject": "Re: {thread_subject}",
        "body": """Hi {name},

I'll leave it here so I'm not cluttering your inbox. If getting more calls from Google becomes a priority for {center}, the free breakdown is yours. Just reply "send it".

All the best,
{sender_first}

""" + SHORT_FOOTER,
    },
    "ack": {
        "subject": "Re: {thread_subject}",
        "body": """Hi {name},

Great, thank you. I'll put it together and send it over within 2 business days.

{sender_first}""",
    },
    "audit": {
        "subject": "Re: {thread_subject}",
        "body": """Hi {name},

Here it is: the one-page breakdown for {center} (attached). If you only fix one thing, I'd start here: {first_fix}

If you'd like to walk through it together, you can grab a free 30-minute slot here: {calendly}

{sender_first}""",
    },
    "pa_fu1": {
        "subject": "Re: {thread_subject}",
        "body": """Hi {name},

Did you get a chance to look at the breakdown? Happy to answer any questions about it, or walk through it on a quick call: {calendly}

{sender_first}""",
    },
    "pa_fu2": {
        "subject": "Re: {thread_subject}",
        "body": """Hi {name},

Last note from me on this. If you want a hand fixing what's in the breakdown, just reply and we'll take it from there.

All the best,
{sender_first}""",
    },
    "later_ack": {
        "subject": "Re: {thread_subject}",
        "body": """Hi {name},

Understood, thanks for letting me know. I'll check back in a few months.

{sender_first}""",
    },
    "optout_ack": {
        "subject": "Re: {thread_subject}",
        "body": """Understood, I won't contact you again.""",
    },
}


def render(kind: str, **slots) -> tuple[str, str]:
    t = TEMPLATES[kind]
    if kind == "email1" and "offer" not in slots:
        slots["offer"] = OFFERS[slots.get("offer_index", 0) % len(OFFERS)].format(**slots)
    return t["subject"].format(**slots), t["body"].format(**slots)


def base_slots(cfg: dict) -> dict:
    s, c = cfg["sender"], cfg["company"]
    return dict(sender_full=s["name"], sender_first=s.get("first_name") or (s["name"].split() or [""])[0],
                company=c["name"], website=c.get("website", ""), tagline=c["tagline"],
                address=c["mailing_address"], calendly=c["calendly"])
