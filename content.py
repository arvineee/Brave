"""All copy and form options for Mapenzi ya 30. Edit text here; no code changes needed."""

DEFAULT_SCORE = 3

SITE = {
    "org": "Jagora Health Organization",
    "place": "Murang'a County, Kenya",
    "eyebrow": "Mapenzi ya 30",
    "hero_title": "How was your week, really?",
    "hero_lead": "A private, three-minute weekly check-in for students. No appointments, "
    "no judgment, just honest reflection, and support when you want it.",
    "about": "#IAMBRAVE is a mental wellness initiative by Jagora, a health organization "
    "based in Murang'a County, Kenya. We believe every young person deserves a simple, "
    "private way to check in with themselves: no appointments, no judgment, just honest "
    "reflection. We exist because mental wellness is not a luxury; it is the foundation "
    "everything else is built on.",
    "steps": [
        ("Check in", "Answer a few quick questions about your energy, sleep and week. "
         "It takes less than 3 minutes."),
        ("Get your response", "See a message that fits where you are right now, with one "
         "small thing to try."),
        ("Reach support", "Ask to be connected with Brave, or call Befrienders Kenya any time."),
    ],
    "privacy": [
        ("No accounts, no cookies, no tracking", "You never log in and we never ask for your name."),
        ("Scored on your phone", "Your response is worked out on your device, and what you write "
         "in the reflection boxes never leaves it."),
        ("Only the basics are saved", "We save your score and multiple-choice answers, without "
         "any personal details, to understand how students are doing."),
        ("Contact only if you choose", "If you ask for support and give a contact, it is stored "
         "separately from your answers, only so Brave can reach you."),
    ],
}

RESPONSES = {
    1: {
        "title": "Thriving",
        "tone": "thriving",
        "message": "It’s wonderful to see you doing well this week! Take a moment to "
        "appreciate your rhythm. Consider sharing your positive energy to support "
        "someone around you, and check out what's happening at Brave Hub.",
    },
    2: {
        "title": "Holding Steady",
        "tone": "steady",
        "message": "You're showing up and keeping things steady, and that takes genuine "
        "effort. Give yourself credit for navigating your week. Try taking one small "
        "action just for yourself today—whether it's a short walk or a quiet breath.",
    },
    3: {
        "title": "Noticing Strain",
        "tone": "strain",
        "message": "It sounds like you might be carrying a bit of strain right now, and "
        "that is completely normal for student life. You don't have to carry it all "
        "alone; consider looping in a trusted friend or checking out our support options.",
    },
    4: {
        "title": "Struggling",
        "tone": "struggling",
        "message": "We hear how heavy this week feels for you. Acknowledging that weight "
        "is a brave first step. Please look through the support options below—reaching "
        "out is a sign of strength.",
    },
    5: {
        "title": "In Crisis",
        "tone": "crisis",
        "message": "Your safety and well-being matter most right now. Please connect "
        "immediately with the crisis support resources above. You do not have to walk "
        "through this alone.",
    },
}

LOCKED = {
    "title": "Mapenzi ya 30 is paused for now",
    "message": "The weekly check-in is not available right now. Support is still here "
    "for you, and the numbers below work at any time.",
    "admin_heading": "Site administrator",
    "admin_help": "Enter the licence key to continue using the site.",
    "key_label": "Licence key",
    "button": "Unlock",
    "wrong_key": "That key was not accepted. Please check it and try again.",
    "too_many": "Too many attempts. Please wait a few minutes and try again.",
}

CRISIS = {
    "heading": "Support Is Always Available",
    "intro": "If things feel too heavy or you need someone to talk to right now, "
    "please reach out:",
    "name": "Befrienders Kenya",
    "phone_display": "0722 178 177",
    "phone_tel": "+254722178177",
    "helplines": [{"name": "Befrienders Kenya", "phone_display": "0722 178 177",
                   "phone_tel": "+254722178177"}],
    "counsellor_prompt": "You can also speak confidentially to your campus counsellor.",
    "footer_tag": "#IAMBRAVE • Murang'a County",
}

# Check-in form. Each option is (label, points). Higher points = more strain.
# The page sums the points in the browser and maps the total to a score 1-5
# (see static/scoring.js). NOTE: points and bands are a DRAFT rubric and need
# Brave's clinical sign-off.
FORM = {
    "energy": [("High.", 0), ("Balanced.", 0), ("I'm okay but tired", 1), ("Low", 2), ("Exhausted.", 3)],
    "sleep": [("7+", 0), ("5-6.", 1), ("3-4.", 2), ("Less than 3", 3)],
    # Multi-select; the browser caps the combined contribution at 2.
    "kept_up": [
        ("Screen time.", 0),
        ("School deadlines", 1),
        ("Money worries.", 1),
        ("Thoughts wouldn’t slow down", 1),
        ("Caffeine/Substances.", 1),
        ("Social stuff", 0),
        ("I slept fine.", 0),
    ],
    "support_now": [("Yes", 3), ("No", 0), ("Maybe.", 1)],
    "support_type": [
        ("Personal therapy.", 0),
        ("Learning about mental wellness.", 0),
        ("Support group/ Group sessions.", 0),
        ("Movement. (Workouts, dancing, outdoor activities)", 0),
        ("Other:", 0),
    ],
    "channel": [("SMS", 0), ("WhatsApp.", 0), ("Instagram.", 0), ("Email.", 0)],
}

# Answers of question 7 that open the support questions.
WANTS_SUPPORT = ("Yes", "Maybe.")

# ---- Wording of the ten built-in questions (editable from /admin/questions) ----
# The number ("1.") is added by the template. Scoring options stay in FORM above.
PROMPTS = {
    "q1": "How’s your energy today – honestly?",
    "q2": "How connected do you feel to people at school right now?",
    "q3": "How many hours of sleep did you get last night?",
    "q4": "What mainly kept you up?",
    "q5": "One small win from this week – anything counts",
    "q6": "What made this week feel heavy?",
    "q7": "Would you like any mental wellness support right now?",
    "q8": "What kind of support would help most right now?",
    "q9": "How should support reach you?",
    "q10": "Best contact to reach you on",
}

# Option groups whose labels staff may reword (position and points never change, so the
# scoring rubric stays as signed off). support_now is locked: the form logic depends on it.
# Value = the longest label the database column can hold.
EDITABLE_LABELS = {"energy": 60, "sleep": 30, "kept_up": 40, "support_type": 80, "channel": 20}

# Public contact details shown in the site footer (empty until staff add them).
CONTACTS = {"email": "", "phone": ""}

SOCIAL_PLATFORMS = ("Instagram", "Facebook", "X", "TikTok", "YouTube", "WhatsApp",
                    "LinkedIn", "Website")
