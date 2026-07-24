TONE_PROFILES = {
    "techno_hooligan": {
        "name": "Хроники веселого техно-хулигана",
        "voice": "легко, живо, с самоиронией и ощущением эксперимента",
        "best_for": ["bug", "experiment", "chaos", "reel", "stories"],
    },
    "scrupulous_builder": {
        "name": "Серьезные выводы скрупулезного разработчика",
        "voice": "спокойно, точно, с пользой и ясным выводом",
        "best_for": ["refactor", "architecture", "post", "lesson"],
    },
    "future_builder": {
        "name": "Философия человека, который строит будущее",
        "voice": "глубже, атмосфернее, с размышлением о роли AI в жизни",
        "best_for": ["insight", "daily_note", "brand"],
    },
    "ai_translator": {
        "name": "AI-переводчик сложного мира",
        "voice": "простыми словами, без жаргона, через пользу для обычного человека",
        "best_for": ["technical", "feature", "explain"],
    },
    "behind_the_scenes": {
        "name": "Закулисье создателя",
        "voice": "честно: что получилось, что сломалось, что удивило",
        "best_for": ["build_in_public", "bug", "process"],
    },
    "mini_lesson": {
        "name": "Мини-урок дня",
        "voice": "коротко: проблема, инсайт, вывод",
        "best_for": ["lesson", "post", "hooks"],
    },
    "almost_meme": {
        "name": "Почти мемный формат",
        "voice": "коротко, иронично, с простой формулировкой",
        "best_for": ["caption", "stories", "short_video"],
    },
}


DEFAULT_TONE_ROTATION = [
    "techno_hooligan",
    "ai_translator",
    "behind_the_scenes",
    "scrupulous_builder",
    "mini_lesson",
    "future_builder",
    "almost_meme",
]
