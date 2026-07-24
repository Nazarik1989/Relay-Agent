PRIVACY_PATTERNS = [
    {
        "kind": "email",
        "pattern": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "replacement": "[email скрыт]",
    },
    {
        "kind": "token",
        "pattern": r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9_]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,}|"
        r"xox[baprs]-[A-Za-z0-9-]{10,}|\d{8,12}:[A-Za-z0-9_-]{30,})\b",
        "replacement": "[токен скрыт]",
    },
    {
        "kind": "api_key_assignment",
        "pattern": r"(?i)\b(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]?[^'\"\s]{6,}",
        "replacement": "[секрет скрыт]",
    },
    {
        "kind": "private_url",
        "pattern": r"\bhttps?://(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[0-1])\.\d+\.\d+|[^\s/]+\.internal)[^\s)]*",
        "replacement": "[внутренний URL скрыт]",
    },
    {
        "kind": "ssh_target",
        "pattern": r"(?i)\b(?:ssh\s+)?[A-Za-z0-9._-]+@(?:[A-Za-z0-9.-]+|(?:\d{1,3}\.){3}\d{1,3})\b",
        "replacement": "[SSH-адрес скрыт]",
    },
    {
        "kind": "ip_address",
        "pattern": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
        "replacement": "[IP-адрес скрыт]",
    },
    {
        "kind": "windows_path",
        "pattern": r"\b[A-Za-z]:[\\/](?:Users|Projects|Work|Client)(?:[\\/][^\s)\]}>;,]+)*",
        "replacement": "[локальный путь скрыт]",
    },
    {
        "kind": "server_path",
        "pattern": r"(?<!\w)/(?:root|home|opt|etc|srv|var/(?:lib|log|www))(?:/[^\s)\]}>;,]+)*",
        "replacement": "[серверный путь скрыт]",
    },
    {
        "kind": "ssh_fingerprint",
        "pattern": r"(?i)\b(?:SHA256:[A-Za-z0-9+/]{43}=?(?![A-Za-z0-9+/=])|MD5:(?:[0-9a-f]{2}:){15}[0-9a-f]{2})\b",
        "replacement": "[SSH-отпечаток скрыт]",
    },
]

SENSITIVE_KEYWORDS = [
    "client",
    "customer",
    "nda",
    "internal prompt",
    "production secret",
    "private project",
    "коммерческая тайна",
    "клиент",
    "пароль",
    "секрет",
    "токен",
]
