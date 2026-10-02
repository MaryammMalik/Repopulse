# RepoPulse

**Understand any codebase in seconds.** Paste a GitHub repo URL and get an AI-generated contributor onboarding guide plus an architecture diagram.


**Live app:** http://repopulse-maryam-2026.s3-website-us-east-1.amazonaws.com

## What it does

1. Fetches a repo's file tree, README, languages and stats via the GitHub API
2. Sends them to an LLM (GPT-OSS 120B on Groq) to generate a structured guide:
   What it does, Key components, How it works, Tech stack, Getting started, Where to look first
3. Generates a Mermaid.js architecture diagram, rendered in the browser
4. Caches results in DynamoDB (24h TTL) for instant repeat lookups
5. Lets you download the guide as Markdown

## Why

Joining a new project or reviewing an unfamiliar repo means hours of reading. RepoPulse cuts onboarding to seconds.

## Architecture

```
Browser (S3 static site)
   │  POST { repo_url }
   ▼
AWS Lambda Function URL (Python 3.10, CORS enabled)
   ├── DynamoDB  RepoPulseCache (cache hit → return)
   ├── GitHub REST API (tree, README, languages)
   └── Groq API (openai/gpt-oss-120b)
   ▼
JSON { summary, diagram, cached }
```

| Layer | Tech |
|---|---|
| Frontend | Single-file HTML/CSS/JS, Mermaid.js 10.9, hosted on S3 |
| Backend | AWS Lambda + Function URL, deployed with AWS SAM |
| Database | DynamoDB (TTL cache, key: `repo_url`) |
| AI | Groq, `openai/gpt-oss-120b` |
| Built with | Kiro |

## Project structure

```
├── frontend/index.html   # UI
├── src/
│   ├── app.py            # Lambda handler
│   └── requirements.txt
├── template.yaml         # SAM / CloudFormation
└── README.md
```

## Run it yourself

**Prerequisites:** AWS CLI (configured), SAM CLI, a [Groq API key](https://console.groq.com/keys).

```bash
sam build
sam deploy --guided --parameter-overrides GroqApiKey=<your_key>
```

Copy the Function URL from the stack outputs into `frontend/index.html`, then host the frontend:

```bash
aws s3 cp frontend/index.html s3://<your-bucket>/index.html --content-type text/html
```

Or open `frontend/index.html` locally (e.g. VS Code Live Server).

## API

`POST <function-url>`

```json
{ "repo_url": "https://github.com/owner/repo" }
```

Response:

```json
{ "repo_url": "...", "summary": "...", "diagram": "flowchart TD ...", "cached": false }
```

## Notes

- Model IDs change often; verify availability at console.groq.com/docs/models.
- `samconfig.toml` is gitignored because it contains the API key. Never commit it.
- Private repos and very large repos are not supported.

## Author

Maryam Malik · [GitHub](https://github.com/MaryammMalik/Repopulse)