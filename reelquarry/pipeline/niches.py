"""Audience presets. Each one tunes what counts as a great moment, the hook
voice, hashtags and default look — the main thing that separates Reelquarry
from generic clippers."""
from __future__ import annotations

NICHES: dict[str, dict] = {
    "podcast": {
        "label": "Podcasters",
        "blurb": "Hot takes, stories and quotable one-liners from long conversations.",
        "guidance": (
            "Favour self-contained stories, strong opinions, surprising facts, funny "
            "exchanges and quotable one-liners. A clip must make sense to someone who "
            "has never heard the show. Avoid housekeeping (sponsors, intros, 'subscribe')."
        ),
        "keywords": ["story", "never", "honestly", "crazy", "truth", "secret", "mistake",
                     "biggest", "worst", "best", "changed", "realised", "realized", "nobody"],
        "hashtags": ["podcast", "podcastclips", "storytime"],
        "style": "bold",
        "layout": "auto",
    },
    "youtuber": {
        "label": "YouTubers",
        "blurb": "Shorts that tease the full video and pull viewers back to the channel.",
        "guidance": (
            "Pick moments with high energy, reveals, results, before/after, reactions "
            "and cliffhangers that make viewers want the full video. Hooks should create "
            "curiosity without clickbait lies."
        ),
        "keywords": ["insane", "result", "tried", "finally", "watch", "challenge", "first",
                     "wow", "test", "actually", "reveal", "won", "lost"],
        "hashtags": ["shorts", "youtubeshorts", "viral"],
        "style": "bold",
        "layout": "auto",
    },
    "property_uk": {
        "label": "UK property agents",
        "blurb": "Viewings, market updates and buyer/seller tips that win instructions.",
        "guidance": (
            "Audience: UK home buyers, sellers, landlords and tenants. Favour practical "
            "tips (stamp duty, mortgages, EPC ratings, surveys, chains, offers, rental "
            "yields), standout property features from tours, local area insight and "
            "market updates. Use UK English and £. Never invent prices or legal/financial "
            "promises; hooks must stay accurate and compliant (no guaranteed returns)."
        ),
        "keywords": ["stamp duty", "mortgage", "offer", "buyers", "sellers", "landlord",
                     "tenant", "rent", "yield", "epc", "survey", "chain", "asking price",
                     "valuation", "garden", "kitchen", "bedroom", "freehold", "leasehold",
                     "market", "interest rate", "first-time", "£"],
        "hashtags": ["ukproperty", "propertytips", "estateagent", "househunting"],
        "style": "boxed",
        "layout": "auto",
    },
    "coach": {
        "label": "Coaches & course creators",
        "blurb": "Bite-size lessons and frameworks that sell the full programme.",
        "guidance": (
            "Favour clear frameworks, step-by-step advice, mindset reframes, client "
            "transformations and contrarian lessons. Every clip should teach one thing "
            "and leave the viewer wanting the full course."
        ),
        "keywords": ["step", "framework", "mistake", "lesson", "clients", "simple", "rule",
                     "secret", "habit", "mindset", "stop", "start", "results", "first"],
        "hashtags": ["coaching", "mindset", "businesstips"],
        "style": "karaoke",
        "layout": "auto",
    },
    "agency": {
        "label": "Marketing agencies",
        "blurb": "Volume-ready clips with brand watermarks for every client.",
        "guidance": (
            "Pick on-brand, polished moments with clear value propositions, social proof, "
            "stats and punchy statements suitable for a client's paid and organic social."
        ),
        "keywords": ["customers", "growth", "roi", "percent", "%", "brand", "launch",
                     "results", "strategy", "campaign", "data", "increase"],
        "hashtags": ["marketing", "socialmedia", "business"],
        "style": "minimal",
        "layout": "auto",
    },
    "general": {
        "label": "Everyone else",
        "blurb": "Balanced settings for any talking-head or interview video.",
        "guidance": "Pick the most engaging, self-contained and shareable moments.",
        "keywords": ["never", "secret", "best", "worst", "mistake", "truth", "how", "why"],
        "hashtags": ["fyp", "shorts", "reels"],
        "style": "bold",
        "layout": "auto",
    },
}


def get_niche(key: str | None) -> dict:
    return NICHES.get(key or "general", NICHES["general"])


def public_niches() -> list[dict]:
    return [{"key": k, "label": v["label"], "blurb": v["blurb"],
             "style": v["style"], "layout": v["layout"]} for k, v in NICHES.items()]
