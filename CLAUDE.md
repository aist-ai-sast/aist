# AI Agent Rules

- Ask before delete files
- Prefer minimal diffs
- This is a Django project, which launches SAST pipelines on imported project. Analyses performed in docker environment.
- Follow existing code style from ruff
- You can use bash commands to analyze project, to launch ruff. But do not call bash commands to change global settings or read/write files outside project. 
- Find the CI plan in the .github/workflows folder.
- Add or update tests for the code you change, even if nobody asked. Tests must reflect user-scenario, not sintetic smoke scenario.
- Dev environment ARM MacOs. Prod environment Ubuntu amd64
- Change of files from vendor is prohibited
- Don't try to invent solution from scratch, try to use already developed and popular solutions.
- Implemented solutions must be secure, efficient, flexible and reusable.

## Frontend rules

- Frontend responsibility is to show data from backend, no complex logix should be implemented in UI
- Use PermissionGate for controls, which should be available only for Users with Write permissions
- All elements must be of the same style as the previous elements of such type.
- The design must align with the business enterprise level.

## Security rules

- Keep user limitations in mind. User, projects, integarations belongs to Organizations. 
- It's absolutely restricted that User of one organization can access some data of other organization. 
- The exception is super-user, who can see all data.
- On each check of docker configuration check that it meets security criteria.


## Backend rules

- Implemented solution must be free of dead-locks, race condition.
- Write import statements on top of file, not inside function scope.


## Tests

- To launch full test suite you can use scripts run-rest-framework-tests.zsh --clean and run-client-ui-tests.zsh --clean. WARNING: this really long action, use it with caution.
- Don't try to launch tests/npm/npx locally, all requirement environment is in docker containers not locally on host.
