# kb-web — 知识库 Web 服务

基于 Hindsight 向量数据库 + DeepSeek LLM 的个人知识库系统。

文档上传 → 智能解析（PDF/Word/Markdown）→ 向量化入库 → 自然语言搜索 → AI 合成答案。

## 架构

```
浏览器 / API 客户端
        │
        ▼
┌─────────────────────────────────┐
│  kb-web (FastAPI :3002)         │
│  ┌──────────┐ ┌──────────────┐ │
│  │ 文档解析  │ │ 搜索 & 问答   │ │
│  │ pypdf    │ │ DeepSeek API │ │
│  │ MinerU   │ │              │ │
│  │ tesseract│ │              │ │
│  └──────────┘ └──────────────┘ │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  Hindsight (:8888)              │
│  PostgreSQL + pgvector          │
│  Embeddings + Reranker          │
└─────────────────────────────────┘
```

## 前置依赖

| 服务 | 用途 | 必需 |
|------|------|------|
| [Hindsight](https://github.com/vectorize-io/hindsight) | 向量存储 & 语义召回 | ✅ 必须 |
| PostgreSQL 16 + pgvector | Hindsight 后端 | ✅ 必须 |
| DeepSeek API | 问答生成 | ✅ 必须 |
| MinerU API | 扫描件 PDF 表格识别 | 强烈建议 |
| tesseract-ocr | OCR 回退（扫描件无 MinerU 时） | 可选 |

> **注意**: DeepSeek 没有 Embeddings 模型。Hindsight 的 Embeddings provider 需要单独配置（推荐硅基流动/智谱 embedding-2）。详见 Hindsight 部署文档。

## 一键部署

```bash
git clone https://github.com/ROOT2713/kb-web.git
cd kb-web
bash setup.sh
```

`setup.sh` 会：
1. 安装系统依赖（poppler-utils, tesseract-ocr）
2. 创建 Python venv 并安装依赖
3. 检查 Hindsight 和 API Key 状态
4. 可选安装 systemd 服务

## 手动部署

### 1. 系统依赖

```bash
sudo apt install poppler-utils tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng python3-venv
```

### 2. Python 依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 环境变量

在 `~/.hermes/.env` 中配置：

```bash
DEEPSEEK_API_KEY=sk-xxx        # DeepSeek API Key（问答生成）
MINERU_API_TOKEN=eyJxxx        # MinerU API Token（扫描件表格识别）
```

### 4. 启动 Hindsight

确保 Hindsight 运行在 `localhost:8888`：

```bash
curl http://localhost:8888/health
# {"status":"healthy","database":"connected"}
```

### 5. 启动 kb-web

```bash
# 临时启动（前台）
python3 server.py

# systemd 启动（生产）
sudo cp kb-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kb-web
```

访问 `http://localhost:3002`

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web UI（拖拽上传 + 搜索） |
| POST | `/api/upload` | 上传文档（multipart: file + title + category） |
| POST | `/api/query` | 搜索问答（form: q=问题） |
| GET | `/api/documents` | 列出所有文档 |
| PATCH | `/api/documents/{id}` | 修改文档标题/分类 |
| DELETE | `/api/documents/{id}` | 删除文档及其向量 |
| GET | `/api/stats` | 知识库统计 |
| POST | `/api/reparse/{id}` | 重新解析文档（MinerU 升级后用） |

## 支持的文件格式

- PDF（文字层 / 扫描件 OCR，扫描件建议配 MinerU API）
- Word (.docx, .doc)
- Markdown (.md)
- 纯文本 (.txt)

## 技术栈

- Python 3.11+ / FastAPI / uvicorn
- Hindsight (向量检索 + SQLite 元数据)
- DeepSeek Chat API (答案生成)
- MinerU API (高精度 PDF 表格识别)
- Tesseract OCR (回退方案)
