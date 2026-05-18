"""会话存档消息持久化 (SQLite)。

- chat_messages: 解密后的会话消息(seq 主键去重)
- cursor: 拉取游标(name+seq, 唯一行,断点续拉)
"""
import json
import sqlite3
from pathlib import Path
from typing import Optional


class Storage:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit 模式;后台拉取写入压力小,够用
        self.conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                seq         INTEGER PRIMARY KEY,
                msgid       TEXT UNIQUE,
                received_at REAL DEFAULT (strftime('%s','now')),
                from_user   TEXT,
                to_users    TEXT,
                msg_type    TEXT,
                raw_json    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_received_at
                ON chat_messages(received_at);

            CREATE TABLE IF NOT EXISTS cursor (
                name TEXT PRIMARY KEY,
                seq  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rag_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT NOT NULL,
                best_answer TEXT NOT NULL,
                tags        TEXT,
                source      TEXT,
                created_by  TEXT,
                created_at  REAL DEFAULT (strftime('%s','now')),
                updated_at  REAL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_rag_category ON rag_entries(category);

            CREATE TABLE IF NOT EXISTS admins (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL,
                created_at    REAL DEFAULT (strftime('%s','now')),
                created_by    TEXT
            );

            CREATE TABLE IF NOT EXISTS prompts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                content     TEXT NOT NULL,
                is_default  INTEGER NOT NULL DEFAULT 0,
                created_at  REAL DEFAULT (strftime('%s','now')),
                updated_at  REAL DEFAULT (strftime('%s','now')),
                updated_by  TEXT
            );

            CREATE TABLE IF NOT EXISTS ai_drafts (
                customer_id TEXT PRIMARY KEY,
                query       TEXT NOT NULL,
                answer      TEXT NOT NULL,
                last_seq    INTEGER DEFAULT 0,    -- 该轮最后一条家长消息的 seq, 用来判断草稿是否过期
                updated_at  REAL DEFAULT (strftime('%s','now')),
                updated_by  TEXT
            );
            -- 兼容老库: 加列(已存在会报错被忽略)


            CREATE TABLE IF NOT EXISTS attachments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id      INTEGER NOT NULL,
                file_path     TEXT NOT NULL,
                mime_type     TEXT NOT NULL,
                original_name TEXT,
                size_bytes    INTEGER NOT NULL DEFAULT 0,
                kind          TEXT NOT NULL,    -- 'image' | 'video'
                created_at    REAL DEFAULT (strftime('%s','now')),
                created_by    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_attachments_entry ON attachments(entry_id);

            CREATE TABLE IF NOT EXISTS attachment_wxwork_media (
                attachment_id INTEGER PRIMARY KEY,
                media_id      TEXT NOT NULL,
                uploaded_at   REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at REAL DEFAULT (strftime('%s','now')),
                updated_by TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,    -- 'draft_adopt' | 'draft_regen' | 'draft_generated' | 'rag_retrieve'
                customer_id TEXT,
                staff_id    TEXT,
                data        TEXT,             -- JSON 业务字段
                created_at  REAL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_customer  ON events(customer_id);

            CREATE TABLE IF NOT EXISTS candidate_phrases (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_query        TEXT NOT NULL,
                staff_reply         TEXT NOT NULL,
                cleaned_reply       TEXT,
                suggested_category  TEXT,
                suggested_variants  TEXT,    -- JSON array
                llm_score           REAL,
                llm_reason          TEXT,
                source_seq          INTEGER,
                customer_id         TEXT,
                status              TEXT DEFAULT 'pending',  -- pending | adopted | ignored | merged
                reviewed_by         TEXT,
                reviewed_at         REAL,
                rag_entry_id        INTEGER,
                reply_hash          TEXT UNIQUE,
                created_at          REAL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidate_phrases(status);

            CREATE TABLE IF NOT EXISTS rag_answer_vectors (
                entry_id     INTEGER PRIMARY KEY,
                vector       BLOB NOT NULL,
                embedded_at  REAL DEFAULT (strftime('%s','now'))
            );
            """
        )
        # 老库兼容: 给 ai_drafts 加 last_seq 列(忽略已存在错误)
        try:
            self.conn.execute("ALTER TABLE ai_drafts ADD COLUMN last_seq INTEGER DEFAULT 0")
        except Exception:
            pass
        # 老库兼容: candidate_phrases 加 2 列(忽略已存在错误)
        for col_sql in (
            "ALTER TABLE candidate_phrases ADD COLUMN suggested_merge_entry_id INTEGER",
            "ALTER TABLE candidate_phrases ADD COLUMN answer_match_similarity  REAL",
        ):
            try:
                self.conn.execute(col_sql)
            except Exception:
                pass
        self._seed_admins()
        self._seed_prompts()

    def _seed_admins(self):
        """从 ADMIN_INIT_USERNAME / ADMIN_INIT_PASSWORD 环境变量初始化首个超管账号。

        - 已有任何 admin 记录就不再 seed (避免覆盖)
        - 没配环境变量就不 seed (用户自己跑 admin_init.py 创建)
        - 永不在代码里硬编码账号密码
        """
        import os
        existing = self.conn.execute("SELECT COUNT(*) FROM admins").fetchone()
        if existing and existing[0] > 0:
            return
        username = (os.environ.get("ADMIN_INIT_USERNAME") or "").strip()
        password = os.environ.get("ADMIN_INIT_PASSWORD") or ""
        if not username or not password:
            import logging
            logging.getLogger(__name__).warning(
                "admins 表为空且未配置 ADMIN_INIT_USERNAME/ADMIN_INIT_PASSWORD,"
                "请用 admin_init.py 创建首个超管账号"
            )
            return
        import bcrypt
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        self.conn.execute(
            "INSERT INTO admins (username, password_hash, role, created_by) VALUES (?, ?, ?, ?)",
            (username, hashed, "super", "system"),
        )

    def _seed_prompts(self):
        """seed 默认 AI 生成 prompt(name='ai_draft', is_default=1)。已存在不覆盖。"""
        from app.prompts_default import AI_DRAFT_DEFAULT_PROMPT
        existing = self.conn.execute(
            "SELECT 1 FROM prompts WHERE name = ?", ("ai_draft",)
        ).fetchone()
        if existing:
            return
        self.conn.execute(
            "INSERT INTO prompts (name, content, is_default, updated_by) VALUES (?, ?, ?, ?)",
            ("ai_draft", AI_DRAFT_DEFAULT_PROMPT, 1, "system"),
        )

    def get_cursor(self, name: str = "default") -> int:
        row = self.conn.execute(
            "SELECT seq FROM cursor WHERE name=?", (name,)
        ).fetchone()
        return row[0] if row else 0

    def set_cursor(self, seq: int, name: str = "default"):
        self.conn.execute(
            "INSERT INTO cursor(name, seq) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET seq=excluded.seq",
            (name, seq),
        )

    def save_message(self, seq: int, msgid: str, msg: dict) -> bool:
        """返回是否真插入(False = 已存在,被 INSERT OR IGNORE 跳过)。"""
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO chat_messages "
            "(seq, msgid, from_user, to_users, msg_type, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                seq,
                msgid,
                msg.get("from"),
                json.dumps(msg.get("tolist") or [], ensure_ascii=False),
                msg.get("msgtype"),
                json.dumps(msg, ensure_ascii=False),
            ),
        )
        return cursor.rowcount > 0

    def latest_messages(self, limit: int = 10):
        return self.conn.execute(
            "SELECT seq, msgid, received_at, from_user, msg_type, raw_json "
            "FROM chat_messages ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def total_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()
        return row[0] if row else 0


_storage_instance: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage_instance
    if _storage_instance is None:
        from app.config import settings
        _storage_instance = Storage(settings.db_path)
    return _storage_instance
