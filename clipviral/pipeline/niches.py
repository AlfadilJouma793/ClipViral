"""Audience presets. Each one tunes what counts as a great moment, the hook
voice, hashtags and default look — the main thing that separates ClipViral
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
    "tiktok": {
        "label": "TikTok creators",
        "blurb": "Fast, punchy clips that hook in the first second and loop well.",
        "guidance": (
            "Optimise for TikTok's For You page: the first sentence must hook within about "
            "one second (bold claim, surprising fact, conflict or a question). Prefer high "
            "energy, humour, relatable moments, storytime and quick tips. Shorter is better; "
            "favour clips that end on a punchline or a line that makes people rewatch or comment. "
            "Write casual, conversational captions in the creator's voice."
        ),
        "keywords": ["pov", "wait", "crazy", "insane", "story", "never", "actually", "secret",
                     "hack", "tip", "nobody", "trend", "honestly", "literally", "watch"],
        "hashtags": ["fyp", "foryou", "tiktok", "viral"],
        "style": "bold",
        "layout": "auto",
        "lengths": [15, 30],
    },
    "instagram": {
        "label": "Instagram creators",
        "blurb": "Polished, relatable Reels people save and share.",
        "guidance": (
            "Optimise for Instagram Reels: favour relatable, aspirational or genuinely useful "
            "moments that viewers will save or send to a friend: clear tips, mini-lessons, "
            "emotional or inspiring lines, and visually strong moments. The hook should promise a "
            "payoff. Captions: a short first line that works as a headline, a call to action "
            "(save / share / follow) and relevant hashtags."
        ),
        "keywords": ["tip", "save", "share", "how", "why", "life", "mindset", "routine",
                     "lesson", "favourite", "favorite", "love", "best", "mistake"],
        "hashtags": ["reels", "instagramreels", "explorepage", "reelsinstagram"],
        "style": "karaoke",
        "layout": "auto",
        "lengths": [20, 60],
    },
    "x": {
        "label": "X (Twitter)",
        "blurb": "Opinionated, newsworthy clips that spark replies and reposts.",
        "guidance": (
            "Optimise for X (Twitter): favour strong opinions, hot takes, news commentary, "
            "debates, surprising data and quotable statements that invite replies and reposts. "
            "Many people watch with the sound off, so the clip must make sense from captions "
            "alone. Hooks should read like a sharp headline. Keep post text under 280 characters "
            "and use at most 1-2 hashtags."
        ),
        "keywords": ["think", "wrong", "truth", "actually", "debate", "news", "data", "percent",
                     "%", "economy", "policy", "unpopular", "opinion", "nobody", "problem"],
        "hashtags": ["news", "opinion"],
        "style": "minimal",
        "layout": "auto",
        "lengths": [30, 90],
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
             "style": v["style"], "layout": v["layout"], "lengths": v.get("lengths")}
            for k, v in NICHES.items()]
