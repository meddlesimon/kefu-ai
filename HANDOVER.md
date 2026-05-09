# 教培 AI 客服助手 · 项目交接文档

> **目的**：给后续接手的 AI 或开发者一份自包含的项目状态快照。
> 不依赖任何对话历史，看完本文档 + `/Users/a1-6/教培AI客服助手-需求文档.md` 即可独立接手。
>
> **最后更新**：2026-04-27
> **当前阶段**：M0（行政开通）即将完成；M1（服务端拉取链路）尚未开始

---

## 0. 凭证机密分级（接手者必读）

| 类型 | 是否机密 | 存放位置 | 是否可入 Git |
|---|---|---|---|
| corpid / agentid | 非机密 | 任意（本文档/Git/.env 都可） | ✅ |
| **app_secret / archive_secret** | **高机密** | 仅服务器 `.env`（chmod 600） | ❌ 永不 |
| **RSA 私钥** | **极高机密** | `/etc/secrets/...`（chmod 600，仅服务进程读） | ❌ 永不 |
| RSA 公钥 | 非机密 | 上传到企微后台 + 本地 `secrets/public_key.pem` 副本 | ✅ |

**强制规则**：本文档**只记录非机密值**，机密只记录"存放位置"。任何看到机密被写进 `.md` / Git / 公开聊天的，都属违规，立即换密钥。

---

## 1. 项目快照

| 项 | 值 |
|---|---|
| 业务 | K12 教培 AI 客服助手（家长沟通） |
| 形态 | 企微 sidebar 插件（H5）+ 服务端（FastAPI） |
| 客服规模 | 1 人（项目所有者本人："北大叶子老师"，企微名"家长多、请稍等"） |
| 服务器 | 腾讯云 118.25.186.95（境内，数据不出境） |
| 端口 | 8098（防火墙已开） |
| 80 / 443 防火墙 | ⚠️ **待确认**（部署 HTTPS 必须开） |
| 域名 | `kefu.sunyeupupup.com`（DNS A 记录已加，等生效 5-30 min） |
| 代码位置 | `/Users/a1-6/Desktop/所有代码/智能客服/`（本文件所在目录） |
| 完整需求文档 | `/Users/a1-6/教培AI客服助手-需求文档.md` |

---

## 2. 企微凭证总账

| 凭证 | 状态 | 值 / 位置 |
|---|---|---|
| **corpid** | ✅ 已拿 | `wwab1dc17860797922` |
| **agentid** | ✅ 已拿 | `1000003`（应用名"学习机AI班主任"） |
| **app_secret** | ✅ 已拿（机密，本文档不记录值） | 部署后写入服务器 `.env` 的 `WXWORK_APP_SECRET` |
| **archive_secret** | ✅ 已拿（机密，本文档不记录值） | 部署后写入服务器 `.env` 的 `WXWORK_ARCHIVE_SECRET` |
| **RSA 私钥** | ❌ 未生成 | 生成后放 `/etc/secrets/wxwork_archive_private_key.pem`（chmod 600） |
| **RSA 公钥** | ❌ 未生成 | 生成后上传到企微「会话内容存档 → 公钥配置」 |

> **接手 AI / 开发者**：app_secret 和 archive_secret 不要重新申请（重新生成会让旧值失效），向项目所有者索取本地备份。

---

## 3. 企微管理后台 · 配置全景

后台分**两套独立配置**：（A）自建应用「学习机AI班主任」 + （B）会话内容存档。两套都要配。

### 3.A 自建应用「学习机AI班主任」

| 字段 | 值 / 状态 |
|---|---|
| AgentId | `1000003` |
| 可见范围 | 4 人（北大叶子老师 / 孙童老师 / 叶子老师 / 北大孙叶-叶骏翔老师） |
| **真实客服** | 北大叶子老师本人（其他 3 人只是"工作台能看到入口"，不实际使用） |
| 管理员 | 北大叶子老师 |
| 应用启用开关 | ✅ 开 |

#### 3.A.1 必配 4 个开发者接口（部署 HTTPS 后回来配）

| # | 模块 | 待填值 | 状态 |
|---|---|---|---|
| 1 | 配置到聊天工具栏 | sidebar 入口 URL：`https://kefu.sunyeupupup.com/sidebar?external_userid=$external_userid&user_id=$user_id` | ⏸ 等 sidebar UI（二期） |
| 2 | 网页授权及JS-SDK → 设置可信域名 | `kefu.sunyeupupup.com`（WW_verify_G1W7bCcJK7bNBwHM.txt 已放服务器 `/var/www/wxwork-verify/`） | ✅ 2026-04-27 |
| 3 | 企业微信授权登录 | 回调域名 `kefu.sunyeupupup.com` | ✅ 2026-04-27 |
| 4 | 企业可信IP | `118.25.186.95` | ✅ 2026-04-27 |

### 3.B 会话内容存档（独立 SaaS 服务）

| 字段 | 值 / 状态 |
|---|---|
| 版本 | **服务版**（不要选企业版，企业版是全员存档） |
| 开启范围 | 1 人（北大叶子老师本人） |
| 免费体验截止 | **2026-05-27 23:59**（30 天）；之后 ¥95/年 |
| 是否已点「立即开启」 | ✅ 2026-04-27 启用，30 天倒计时启动 |

#### 3.B.1 必配 3 项

| # | 必填项 | 待填值 | 状态 |
|---|---|---|---|
| 1 | 设置接收事件服务器 | `https://kefu.sunyeupupup.com/wxwork/event` | ⏸ **本期跳过**：走主动拉模式（GetChatData seq），不依赖企微推送。后续要做需实现 echostr 校验端点 |
| 2 | 设置可信 IP 地址 | `118.25.186.95` | ✅ |
| 3 | 设置消息加密公钥 | RSA 2048 X.509 PEM | ✅ 用文件直接 pbcopy 粘贴成功 |

填完 3 项 → 点「立即开启」→ 30 天倒计时启动。

---

## 4. DNS / HTTPS / 部署链

| 步骤 | 状态 |
|---|---|
| DNS A 记录 `kefu` → `118.25.186.95` | ✅ 生效 |
| 80 / 443 防火墙（IPv4） | ✅ |
| 8098 端口防火墙 | ✅ |
| Docker 27.5.1 + Compose v2.32.4 | ✅ |
| 服务器构建目录 | ✅ `/home/ubuntu/kefu-ai/` |
| Nginx 反代（443 → 8098） | ✅ `/etc/nginx/sites-enabled/kefu.sunyeupupup.com` |
| Let's Encrypt 证书 | ✅ 到期 2026-07-26，certbot 自动续期 |
| 容器 `kefu-backend` | ✅ Up，监听 0.0.0.0:8098 |
| `access_token` 凭证联通 | ✅ 启动时已成功获取（验收第 1 条） |
| HTTPS `https://kefu.sunyeupupup.com/health` | ✅ 200 OK |

---

## 5. 项目目录规划（仅根目录已创建）

```
/Users/a1-6/Desktop/所有代码/智能客服/
├── HANDOVER.md                  ← 本文档（已创建）
├── backend/                     ← FastAPI 服务端
│   ├── app/
│   │   ├── auth/                ← 4 个认证模块（本期范围）
│   │   │   ├── access_token.py
│   │   │   ├── oauth.py
│   │   │   ├── jsapi.py
│   │   │   └── archive_decrypt.py
│   │   ├── main.py
│   │   └── config.py
│   ├── secrets/                 ← 私钥（gitignore）
│   │   └── private_key.pem
│   ├── .env                     ← 凭证（gitignore）
│   ├── .env.example             ← 模板（入库）
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/                    ← Vue3 sidebar（后续做）
├── docker-compose.yml
├── .gitignore
└── README.md
```

---

## 6. 4 个核心认证模块（服务端，本期范围）

### 6.1 access_token 缓存器
- 启动时调企微 API 换 token，内存缓存
- 定时器 90 分钟刷新（企微给 7200 秒，留 10 分钟余量）
- 失败重试 3 次后告警
- 暴露 `get_access_token()` 给其他模块
- API：`https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=...&corpsecret=...`

### 6.2 OAuth 登录端点
- 端点：`POST /api/auth/oauth_callback`
- 输入：企微 sidebar 跳转过来的 `code`（URL 参数）
- 流程：code + access_token → 调企微 `auth.getuserinfo` → 拿 `user_id` → 发 JWT 给前端
- JWT 过期：8 小时
- 前端拿 JWT 后存 localStorage，所有后续 API 走 `Authorization: Bearer <JWT>`

### 6.3 JS-SDK 签名端点
- 端点：`GET /api/auth/jsapi_signature?url=<当前页URL>`
- 流程：jsapi_ticket（缓存 7200 秒）+ nonceStr + timestamp + url → SHA1 → signature
- 返回：`{appId, timestamp, nonceStr, signature}` 给前端 `wx.config()` 用
- 前端解锁后可调 `wx.invoke('sendChatMessage')` 把候选话术插入企微聊天框

### 6.4 会话存档解密器
- 工具类（不是接口）
- 依赖企微官方 C++ SDK：`libWeWorkFinanceSdk_C.so`，Python 用 `ctypes` 调
- 流程：
  1. SDK `GetChatData(seq)` 拉一批加密消息
  2. RSA 私钥解 `encrypt_random_key` → 拿到 AES key
  3. AES 解 `encrypt_chat_msg` → 得到 JSON 明文
  4. 写入 PostgreSQL（或先 SQLite，1 客服够用）
  5. 更新 seq 游标到磁盘
- ⚠️ Docker 镜像必须用 `python:3.11-slim`，**不能用 alpine**（C++ SDK 依赖 glibc）
- ⚠️ 拉取要主动定时（5-10 秒一次），企微云端只存 30 天，过期不补

---

## 7. `.env` 模板（实际值在服务器，本文件不含机密）

```bash
# === 非机密（可入 Git，对应 .env.example） ===
WXWORK_CORPID=wwab1dc17860797922
WXWORK_AGENTID=1000003
APP_PORT=8098
DOMAIN=kefu.sunyeupupup.com
JWT_TTL_SECONDS=28800

# === 机密（不入 Git，仅服务器 .env） ===
WXWORK_APP_SECRET=<问项目所有者要>
WXWORK_ARCHIVE_SECRET=<问项目所有者要>
WXWORK_PRIVATE_KEY_PATH=/etc/secrets/wxwork_archive_private_key.pem
JWT_SECRET=<生成一个 32 字节随机串：openssl rand -hex 32>
```

---

## 8. 验收标准（4 条全过 = 认证服务完成）

1. 启动服务后日志显示 `access_token 已获取，过期时间 7200s`
2. 浏览器打开 `https://kefu.sunyeupupup.com/sidebar?code=测试码` 能识别客服身份并显示 user_id
3. 前端 `wx.config(...)` 不报错，能成功调 `wx.invoke('sendChatMessage')`
4. 用真实的会话存档密文，本地能解密成 JSON 明文

---

## 9. 当前阶段进度（按需求文档第 8 节里程碑）

| 里程碑 | 状态 |
|---|---|
| **M0 · 行政开通** | 🟡 凭证齐 + 服务端已上线；7 个后台必填项未填，30 天体验未启动 |
| **M1 · 服务端拉取链路（认证部分）** | 🟢 已部署 https://kefu.sunyeupupup.com，access_token 验证通过；会话存档拉取依赖 C++ SDK，后续做 |
| **M2 · 向量检索快速路径** | ❌ 未开始 |
| **M3 · sidebar 插件** | ❌ 未开始 |
| **M4 · LLM 加工 + AI 兜底** | ❌ 未开始 |
| **M5 · 打磨上线** | ❌ 未开始 |

---

## 10. 下一步任务清单（接手者按此顺序执行）

### 阶段 1 · 本地代码骨架（不依赖部署）
- [x] 建 `backend/`、`frontend/`、`docker-compose.yml`、`.gitignore`、`.env.example`
- [x] 生成 RSA 2048 密钥对（公钥保留本地副本）
- [x] 写 4 个认证模块代码（access_token / oauth / jsapi / archive_decrypt）
- [x] 写 `Dockerfile`（基于 `python:3.11-slim`，pip 用阿里云 mirror）
- [x] ~~本地 `docker compose up`~~（直接上服务器跑，跳过本地）

### 阶段 2 · 部署到 118.25.186.95（2026-04-27 完成）
- [x] 80 / 443 / 8098 防火墙已开（IPv4）
- [x] 服务器已有 Nginx 1.24.0 + certbot 2.9.0
- [x] 申请 `kefu.sunyeupupup.com` Let's Encrypt 证书（2026-07-26 过期，自动续期）
- [x] Nginx 反代 443 → 8098（`/etc/nginx/sites-enabled/kefu.sunyeupupup.com`）
- [x] 代码同步到服务器 `/home/ubuntu/kefu-ai/`，`.env` 已配齐
- [x] Docker 容器 `kefu-backend` 已启动，监听 8098

### 阶段 3 · 企微后台一次性填完 7 项
- [ ] 自建应用 → 企业可信IP → `118.25.186.95`
- [ ] 自建应用 → 网页授权及JS-SDK → 可信域名 → `kefu.sunyeupupup.com`（先放 `WW_verify_xxx.txt` 到服务器根）
- [ ] 自建应用 → 企业微信授权登录 → 设置回调
- [ ] 自建应用 → 配置到聊天工具栏 → sidebar URL
- [ ] 会话内容存档 → 接收事件服务器 → `https://kefu.sunyeupupup.com/wxwork/event`
- [ ] 会话内容存档 → 可信 IP → `118.25.186.95`
- [ ] 会话内容存档 → 消息加密公钥 → 粘贴 RSA 公钥
- [ ] 会话内容存档 → 「立即开启」（**启动 30 天倒计时**）

### 阶段 4 · 联调验收（4 条全过）
- [ ] 4 条验收标准（见第 8 节）

---

## 11. 红线 / 不可妥协约束（来自需求文档）

- ✅ **匹配只走向量检索**（不做字面关键词匹配 —— 家长不会问一模一样的问题）
- ✅ 相似度分级：≥0.90 直接返回历史答案 / 0.70-0.90 调 DeepSeek 加工 / <0.70 提示客服手动
- ✅ **AI 调用必须手动触发**（按钮），不监听消息就自动调
- ✅ **只做 RAG，不做微调**（涉未成年人信息合规雷区）
- ✅ **数据脱敏在入库前做**，不是召回前
- ✅ **AI 不自动按回车发送**，所有消息客服确认
- ❌ 不做 hook / DLL 注入 / 协议逆向
- ❌ 不做企微外的悬浮窗 / 屏幕监听
- ❌ 私钥不进 Git / 聊天工具

---

## 12. 关键陷阱预警（接手者必看）

1. **DNS 生效有延迟**（5-30 分钟），别马上 ping 不通就以为配错了
2. **HTTPS 必须**：企微所有 Webhook 强制 HTTPS，不能用 IP，不能用 HTTP
3. **80 端口要开**：Let's Encrypt 申请走 80 端口的 HTTP-01 验证
4. **Docker 基础镜像**：`python:3.11-slim`，**不要用 alpine**（C++ SDK 依赖 glibc）
5. **WW_verify_xxx.txt**：可信域名校验用，下载后必须以**根路径**（`/WW_verify_xxx.txt`）暴露
6. **可见范围 ≠ 实际客服**：当前可见范围 4 人，但只有北大叶子老师 1 人是真实客服
7. **30 天免费体验**：截至 2026-05-27 23:59，过早启动浪费天数
8. **archive_secret 一旦泄漏**：所有客服-家长对话都能被解，立即去后台「重新获取」换新

---

## 13. 关联系统

- 灵石学员管理：`gaiming.sunyeupupup.com`（118.25.186.95:8095，Flask+Vue 独立容器）—— 后期可打通成绩/课表上下文
- 主页门户：`zhuye.sunyeupupup.com`（118.25.186.95:8097）—— 后期加客服 AI 的入口
- 提词器：`tici.sunyeupupup.com`（8094）
- 主播 PPT：`ppt.sunyeupupup.com`（8096）

---

**文档结束。如有不清楚的地方，先看 `教培AI客服助手-需求文档.md`，再问项目所有者。**
