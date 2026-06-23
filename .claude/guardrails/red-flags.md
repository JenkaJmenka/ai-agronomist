# Red Flags — Immediate STOP WORK

If you see any of these, stop immediately and report to the orchestrator:

1. Any file writing to a path outside its ownership (see CLAUDE.md file ownership table)
2. Any import of os.system, subprocess with shell=True, or eval()
3. Any hardcoded password, token, or API key
4. Any DELETE or DROP in database queries without tenant_id filter
5. Any model.save() without accompanying metadata JSON
6. Test file that imports from and modifies implementation files
7. Circular imports between src/models/ and src/api/
