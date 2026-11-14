"""The approved email templates. Approve these once; the agent only fills the {slots}.

Written for owners and admissions staff, not marketers: everyday words, no SEO jargon.
Format approved by the owner on 2026-11-15: the first email openly says it's a cold email,
gives one real fix for free and asks for nothing. The follow-up rechecks their site and offers
to write up more. Rules baked in: plain text, no links apart from the company domain, no
attachments before they say yes, no em dashes, mailing address and opt-out line in every
cold email, no stock AI phrases (see rules.BANNED).
"""

OPT_OUT = "If I'm bothering you, or you think I've got this wrong, just say so and I won't write again."

FOOTER = """{sender_first}
{company}

""" + OPT_OUT + """
{company}, {address}"""

SHORT_FOOTER = """{sender_first}
{company}

If I'm bothering you, just say so and I won't write again.
{company}, {address}"""

# Used when the day-4 recheck of their site can't load the page.
FU1_NEUTRAL = "I had another look at your site this week."

TEMPLATES = {
    "email1": {
        "subject": "{subject}",
        "body": """{hey},

{opening}

""" + FOOTER,
    },
    "fu1": {
        "subject": "Re: {thread_subject}",
        "body": """{hey},

{fu1_line}

If you want, I'll also check the other searches families use to find a place like {center} and write up what I find, by email. Just say yes.

""" + SHORT_FOOTER,
    },
    "fu2": {
        "subject": "Re: {thread_subject}",
        "body": """{hey},

Last one from me, I promise. If getting more calls from Google ever moves up the list for {center}, just reply and I'll send over what I found.

""" + SHORT_FOOTER,
    },
    "thanks_ack": {
        "subject": "Re: {thread_subject}",
        "body": """{hey},

Glad it helps. If you want, I'll check the other searches families use to find a place like {center} and send you what I find, by email. Just say yes.

{sender_first}""",
    },
    "ack": {
        "subject": "Re: {thread_subject}",
        "body": """{hey},

Great, thank you. I'll put it together and send it over within 2 business days.

{sender_first}""",
    },
    "audit": {
        "subject": "Re: {thread_subject}",
        "body": """{hey},

Here it is: the one-page breakdown for {center} (attached). If you only fix one thing, I'd start here: {first_fix}

If you'd like to walk through it together, you can grab a free 30-minute slot here: {calendly}

{sender_first}""",
    },
    "pa_fu1": {
        "subject": "Re: {thread_subject}",
        "body": """{hey},

Did you get a chance to look at the breakdown? Happy to answer any questions about it, or walk through it on a quick call: {calendly}

{sender_first}""",
    },
    "pa_fu2": {
        "subject": "Re: {thread_subject}",
        "body": """{hey},

Last note from me on this. If you want a hand fixing what's in the breakdown, just reply and we'll take it from there.

All the best,
{sender_first}""",
    },
    "later_ack": {
        "subject": "Re: {thread_subject}",
        "body": """{hey},

Understood, thanks for letting me know. I'll check back in a few months.

{sender_first}""",
    },
    "optout_ack": {
        "subject": "Re: {thread_subject}",
        "body": """Understood, I won't contact you again.""",
    },
}


def greeting(name: str) -> str:
    name = (name or "").strip()
    return "Hey" if not name or name == "there" else f"Hey {name}"


def render(kind: str, **slots) -> tuple[str, str]:
    t = TEMPLATES[kind]
    slots.setdefault("hey", greeting(slots.get("name", "")))
    return t["subject"].format(**slots), t["body"].format(**slots)


def base_slots(cfg: dict) -> dict:
    s, c = cfg["sender"], cfg["company"]
    return dict(sender_full=s["name"], sender_first=s.get("first_name") or (s["name"].split() or [""])[0],
                company=c["name"], website=c.get("website", ""), tagline=c["tagline"],
                address=c["mailing_address"], calendly=c["calendly"])
