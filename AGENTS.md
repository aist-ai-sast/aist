# AI Agent Rules

- Ask before delete files
- Prefer minimal diffs
- This is a Django project, which launches SAST pipelines on imported project. Analyses performed in docker environment.
- Follow existing code style from ruff
- You can use bash commands to analyze project, to launch ruff. But do not call bash commands to change global settings or read/write files outside project. 
- Find the CI plan in the .github/workflows folder.
- Add or update tests for the code you change, even if nobody asked. Tests must reflect user-scenario.
- Dev environment ARM MacOs. Prod environment Ubuntu amd64
- Change of files from vendor is prohibited
- Don't try to invent solution from scratch, try to use already developed and popular solutions.
- Implemented solutions must be secure, efficient, flexible and reusable.

## Skills
### Available skills
- aist-finding-triage: Analyze new findings in an AIST pipeline, classify TP/FP, and produce complete AISTAIFindingResponse payloads with detailed English reasoning and TP reproduction guidance. (file: /Users/butkevichveronika/work/defect_dojo_fork/django-DefectDojo/.codex/skills/aist-finding-triage/SKILL.md)
