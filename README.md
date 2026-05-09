# 教培 AI 客服助手

K12 教培行业的企业微信 AI 客服助手——RAG 知识库 + AI 草稿生成 + 实时家长消息推荐。

## 功能

- **企微 sidebar 插件**：客服打开聊天侧边栏直接看到客户最新轮次消息
- **AI 自动推荐**：家长发完消息 5 秒后,后台自动跑 RAG 检索 + LLM 生成回答草稿
- **一键采用**：客服点一下「采用」,文字塞进企微输入框,回车发出
- **图片/视频附件**：通过企微临时素材接口发送,后台自动续期 mediaId
- **话术挖掘**:扫描客服真实回复,LLM 评估有价值的沉淀为新话术(待审核入库)
- **管理后台**:RAG 知识库 CRUD、prompt 编辑、AI 模型切换、统计面板

## 技术栈

- **后端**：FastAPI + SQLite + asyncio
- **前端**：纯 HTML/JS(sidebar.html, admin.html)
- **RAG**：阿里百炼 text-embedding-v4 (Qwen3-Embedding) + numpy 余弦检索
- **LLM**：DeepSeek v4-flash / 豆包 1.5 Lite / 通义 qwen-plus(可后台切换)
- **会话存档**：企微 SDK `libWeWorkFinanceSdk_C.so` + RSA 解密
- **部署**：Docker + nginx 反代 + Let's Encrypt

## 项目结构

```
backend/
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # pydantic-settings 配置
│   ├── storage.py           # SQLite schema + seed
│   ├── archive_pull.py      # 会话存档轮询拉取(每 3s)
│   ├── auto_ai_draft.py     # 后台自动生成 AI 草稿(5s 防抖)
│   ├── candidate_miner.py   # 话术挖掘(LLM 评估客服回复)
│   ├── llm.py               # 多供应商 LLM 流式客户端
│   ├── api_admin.py         # /api/admin/* 管理后台 API
│   ├── api_ai.py            # /api/ai/* 草稿生成 + SSE 流式
│   ├── api_sidebar.py       # /api/customer/* sidebar 数据
│   ├── api_attachments.py   # 图片/视频附件 CRUD
│   ├── api_events.py        # 埋点 + 统计聚合
│   ├── wxwork_media.py      # 企微临时素材 mediaId 缓存 + 自动续期
│   ├── wxwork_sdk.py        # ctypes 包装 SDK
│   ├── auth/                # OAuth + JS-SDK 签名 + admin 登录
│   ├── rag/                 # RAG 引擎(parser/embed/store/retrieve)
│   └── prompts_default.py   # 默认 system prompt(可在 admin 后台改)
├── static/
│   ├── sidebar.html         # 客服侧边栏 UI
│   └── admin.html           # 管理后台 SPA
├── lib/
│   └── libWeWorkFinanceSdk_C.so  # 企微会话存档 SDK
├── secrets/                 # RSA 私钥(不入库)
├── Dockerfile
├── requirements.txt
└── .env.example             # 配置模板
```

## 快速开始

### 1. 准备账号

- 企业微信认证企业 + 自建应用(Secret + AgentId)
- 客户联系 + 会话存档密钥(配 RSA 公钥)
- 阿里百炼 API key(text-embedding-v4)
- DeepSeek 或 豆包 API key (任选其一)

### 2. 配置

```bash
cd backend
cp .env.example .env
# 编辑 .env 填入凭据
mkdir -p secrets
# 生成 RSA 密钥对
openssl genrsa -out secrets/private_key.pem 2048
openssl rsa -in secrets/private_key.pem -pubout > secrets/public_key.pem
# 把 public_key.pem 内容粘贴到企微会话存档密钥配置
```

### 3. 部署

```bash
docker compose up -d --build
```

服务启动在 `:8098`,前面加 nginx 反代到 HTTPS 域名。

### 4. 企微后台配置

- 应用 → 可信域名:你的域名
- 应用 → 应用主页:`https://your-domain/sidebar`
- 客户联系 → 聊天工具栏 → 自建页:**选「应用主页」**(否则 Windows 客户端会弹外部网页拦截)
- 客户联系 → 「可调用应用」 → 加入此应用(否则 `getCurExternalContact` 报权限错)

### 5. 初始超管账号

两种方式任选其一,**永远不在代码里硬编码账号密码**。

**方式 A** - 在 `.env` 里配置环境变量,首次启动自动 seed:

```bash
ADMIN_INIT_USERNAME=your_admin_name
ADMIN_INIT_PASSWORD=your_strong_password
```

只在 `admins` 表为空时生效;创建后请清掉这两个变量。

**方式 B** - 启动后手动跑 CLI:

```bash
docker exec kefu-backend python admin_init.py <username> <password>
```

不会覆盖已存在的同名账号。密码至少 6 位,bcrypt 哈希入库。

## 关键设计点

- **每条家长消息触发自动 AI 草稿**:`archive_pull` 拉到家长 text → 5 秒防抖 → LLM 流式生成 → 落库 → sidebar 拉到展示
- **AI 一字不差采用统计**:埋点客服点「采用」时存完整 answer + 比对 120s 内客服实发内容,精确知道 AI 帮多少
- **mediaId 缓存 + 自动续期**:企微临时素材 3 天过期,后台每 12h 扫描刷新,客服点发送毫秒级返回
- **话术挖掘**:扫客服回复 → LLM 打分(≥7 分) + 推 category + 4 变体 + 脱敏 → 候选池待审核

## 注意

⚠️ **代码里没有 AI 学习训练**——纯 RAG 检索 + LLM 生成,不存训练数据,不调用任何 fine-tuning。

⚠️ **数据库不要暴露**:SQLite 含会话存档明文,只能本地读。

⚠️ **企微会话存档**:必须客户同意才能存档。法律 / 合规自查。

## 许可

私有项目,内部使用。
