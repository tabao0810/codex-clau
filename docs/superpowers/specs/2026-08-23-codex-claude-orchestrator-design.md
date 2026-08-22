# Thiết kế Codex–Claude Orchestrator

Ngày: 2026-08-23  
Trạng thái: Đã được người dùng duyệt

## 1. Mục tiêu

Xây dựng một CLI độc lập bằng Python, tạm gọi là `codex-claude`, dùng Codex làm
agent lập kế hoạch và review, còn Claude Code là agent duy nhất được sửa source
code trong repository đích. CLI tự chia mục tiêu thành task, chạy các task độc
lập song song trong worktree cách ly, kiểm tra kết quả, yêu cầu sửa lại khi cần
và lưu trạng thái run trong `process.md`.

MVP ưu tiên tính dễ quan sát, khả năng tiếp tục sau gián đoạn, không xung đột
source và giới hạn quyền terminal. MVP không có giao diện web, database, server,
tự commit hoặc tự push.

## 2. Phạm vi và quyết định đã duyệt

- Tool là repository độc lập và dùng được cho nhiều repository đích.
- Runtime là Python 3.11 trở lên.
- Python gọi trực tiếp `codex` và `claude` dưới dạng subprocess; không dùng
  Codex SDK, Claude Agent SDK, MCP hoặc HTTP để nối hai agent.
- Codex chỉ lập kế hoạch và review, luôn chạy read-only.
- Chỉ Claude Code được sửa source code.
- Codex được tạo nhiều task worker cùng lúc khi task không phụ thuộc nhau, không
  dùng chung write scope và không tranh tài nguyên verification.
- Mỗi Claude worker chạy trong một detached Git worktree riêng.
- Mặc định chạy tối đa hai worker, cấu hình bằng `--max-workers`.
- Patch từ worker được kiểm tra và áp dụng tuần tự vào working tree chính.
- Run mới chỉ bắt đầu khi Git working tree sạch.
- Tool không tạo commit. Người dùng review và commit sau khi run kết thúc.
- `process.md` ở root repository đích là trạng thái bền vững và giao diện theo
  dõi tiến trình cho người dùng.
- `resume` đọc `process.md` và tiếp tục bước chưa hoàn tất.
- Mỗi task có tối đa hai lần retry sau lần thực hiện đầu tiên.
- Claude session được rotate khi mức dùng context đạt ngưỡng mặc định 55%.
- Quyền terminal tự động nhưng bị giới hạn; khi cần quyền ngoài policy, run
  chuyển sang `BLOCKED` thay vì bỏ qua permission.

## 3. Giao diện CLI

Hai command chính:

```text
codex-claude run "<mục tiêu>" --cwd <repository>
codex-claude resume --cwd <repository>
```

Các cấu hình vận hành:

```text
--max-workers 2
--claude-context-limit 55
--codex-timeout 600
--claude-timeout 1800
--verification-timeout 900
```

`--max-workers` là số nguyên dương. Giá trị context hợp lệ nằm trong khoảng
1–100. Timeout dùng giây và phải là số nguyên dương. `run` từ chối nếu đã có run
chưa kết thúc; `resume` từ chối nếu không có state hợp lệ.

## 4. Kiến trúc

```text
Mục tiêu người dùng
  -> Python Controller
  -> Codex CLI tạo task DAG và scope
  -> Scheduler chọn các task độc lập
  -> N Claude CLI worker chạy trong worktree riêng
  -> Controller verify trong từng worktree
  -> Codex CLI review evidence của từng task
  -> Controller kiểm tra và apply patch tuần tự
  -> hoàn thành, retry hoặc blocked
```

### 4.1 Controller

Controller là state machine xác định trước, không tự đưa ra quyết định về code.
Nó quản lý subprocess, scheduler, timeout, retry, context rotation, worktree,
patch integration, Git inspection và cập nhật `process.md`.

Controller chạy child process bằng argv với `cwd` cố định, không dùng
`shell=True`. Nó stream stdout/stderr để người dùng quan sát, đồng thời parse
JSONL từ stdout của agent.

### 4.2 CodexAdapter

CodexAdapter gọi `codex exec --json` với sandbox read-only và JSON Schema đóng
gói cùng tool. Lần gọi đầu tạo plan; adapter lấy `thread_id` từ sự kiện
`thread.started`. Các lần review tiếp theo dùng `codex exec resume` với thread
ID này để giữ ngữ cảnh quản lý.

Codex không được sửa file, chạy deployment hoặc thay đổi Git. Codex có thể đề
xuất nhiều task chạy đồng thời, nhưng Scheduler là lớp cuối cùng xác nhận task
thật sự an toàn để chạy song song.

### 4.3 Scheduler

Scheduler validate task DAG, từ chối cycle, xác định tập task đã thỏa dependency
và chọn tối đa `maxWorkers` task. Hai task chỉ được cùng wave khi:

- không phụ thuộc trực tiếp hoặc gián tiếp nhau;
- `writeScopes` không bằng nhau và không chứa nhau;
- `resourceLocks` không giao nhau;
- không task nào sở hữu file toàn cục độc quyền.

Các file như dependency lockfile, migration, database schema và cấu hình root
mặc định là exclusive scope. Task sở hữu một trong các file này chạy một mình
trừ khi plan định nghĩa một policy hẹp hơn đã được Controller chấp nhận.

### 4.4 ClaudeAdapter

ClaudeAdapter gọi `claude -p --output-format stream-json` trong worktree của
worker. Adapter parse system/init, assistant và result events để lấy session ID,
usage, kết quả, permission denial và lỗi.

Claude nhận prompt tự chứa objective, acceptance criteria, repository rules cần
đọc, write scope, verification commands và hạn chế quyền. Claude không nhận raw
transcript của Codex và không được sửa `process.md`.

### 4.5 WorktreeManager và PatchIntegrator

Mỗi worker được tạo bằng detached worktree tại thư mục tạm. Worktree bắt đầu từ
initial HEAD, sau đó replay các patch đã được chấp nhận từ wave trước để thấy
đúng trạng thái code hiện tại.

Sau khi task được verify và Codex review PASS, Controller:

1. thu thập tracked, deleted và untracked change trong worktree;
2. xác nhận changed paths là tập con của `writeScopes`;
3. tạo binary patch và lưu recovery artifact dưới
   `.git/codex-claude/<run-id>/patches/`;
4. chạy `git apply --check` trên working tree chính;
5. áp dụng patch tuần tự;
6. đánh dấu patch đã tích hợp trong `process.md`;
7. chỉ xóa worktree sau khi patch áp dụng thành công.

Tool có thể dùng intent-to-add tạm thời trong index của detached worktree để
đưa file mới vào binary patch, nhưng không stage file trong working tree chính
và không tạo commit. Patch artifact là dữ liệu recovery nội bộ, không phải nguồn
trạng thái task; `process.md` vẫn là state source of truth.

### 4.6 GitInspector

GitInspector:

- xác nhận đường dẫn là Git repository;
- yêu cầu working tree sạch trước run mới;
- lưu initial HEAD;
- lấy changed-file list và diff sau mỗi task;
- phát hiện commit, rebase hoặc thay đổi HEAD trong khi run;
- bảo đảm tool không tự commit hay push.

### 4.7 Verifier

Verifier chạy đúng verification commands trong plan. Command được biểu diễn
thành argv có cấu trúc, không phải chuỗi shell tự do. Output bị giới hạn dung
lượng, lọc secret và gắn exit code, thời gian chạy cùng trạng thái timeout.

Verification task chạy trong worker worktree. `resourceLocks` ngăn hai command
tranh database, port, emulator hoặc dịch vụ dùng chung. Sau mỗi wave tích hợp,
Controller có thể chạy verification liên task trên working tree chính nếu plan
yêu cầu.

### 4.8 ProcessStore

ProcessStore render phần Markdown cho người đọc và một JSON block có version ở
cuối `process.md`. JSON là nguồn state cho `resume`. Update dùng file tạm cùng
filesystem rồi `os.replace()` để tránh file dở dang khi process chết.

Một OS-level lock dành riêng cho repository ngăn hai Controller điều phối cùng
một run. Lock và patch là artifact nội bộ; mọi trạng thái task vẫn nằm trong
`process.md`.

## 5. Giao tiếp giữa Codex và Claude

Codex và Claude không gọi trực tiếp nhau. Python Controller là cầu nối qua:

1. JSON/JSONL trên stdin/stdout của subprocess.
2. Prompt được tạo từ contract có schema.
3. Worktree, Git diff, patch và verification output.

Luồng một task:

```text
Codex plan
  -> task JSON
  -> Scheduler cấp worker
  -> Controller tạo Claude prompt
  -> Claude sửa isolated worktree
  -> Controller lấy diff và chạy verification
  -> Controller gửi evidence cho Codex
  -> Codex trả PASS, RETRY hoặc BLOCKED
  -> Controller kiểm tra và apply patch
  -> Controller cập nhật process.md
```

Controller không chuyển raw transcript giữa hai agent. Log, diff và test output
được giới hạn kích thước và redaction trước khi đưa vào prompt.

## 6. Contract dữ liệu

### 6.1 Planning contract

Codex trả final response theo JSON Schema tương đương:

```json
{
  "summary": "Triển khai đăng nhập",
  "tasks": [
    {
      "id": "T01",
      "title": "Thêm authentication service",
      "objective": "Triển khai service theo quy tắc repository",
      "acceptanceCriteria": ["Test authentication thành công"],
      "dependsOn": [],
      "writeScopes": ["src/auth/", "tests/auth/"],
      "resourceLocks": ["auth-test-database"],
      "parallelSafe": true,
      "verificationCommands": [["pytest", "tests/auth"]],
      "relevantPaths": ["src/auth", "tests/auth"]
    }
  ],
  "finalVerificationCommands": [["pytest"]]
}
```

Mỗi task phải có ID duy nhất, mục tiêu kiểm tra được và phạm vi đủ nhỏ để hoàn
thành trong một Claude session dưới ngưỡng context dự kiến. Command luôn là mảng
argv; schema không chấp nhận command dạng shell string.

`writeScopes` chỉ nhận repository-relative path đã chuẩn hóa, không nhận đường
dẫn tuyệt đối, `..` hoặc glob tùy ý. Scope kết thúc bằng `/` sở hữu toàn bộ cây
con; scope khác sở hữu đúng một file. Hai scope overlap nếu bằng nhau hoặc một
directory scope là prefix theo path segment của scope còn lại.

`resourceLocks` là chuỗi định danh tài nguyên dùng chung như `database:test`,
`port:3000` hoặc `package-manager`. Hai task dùng cùng lock không chạy đồng thời.
Controller không tin `parallelSafe` nếu scope/lock analysis không đạt.

### 6.2 Execution handoff

Prompt cho Claude gồm:

- goal tổng thể và task hiện tại;
- objective và acceptance criteria;
- dependency result cần thiết;
- relevant paths và write scopes;
- repository instruction files cần đọc;
- verification commands;
- thao tác được phép và bị cấm;
- diff/test summary nếu đây là retry hoặc session rotation;
- yêu cầu báo `scope_change_required` thay vì sửa ngoài scope;
- yêu cầu trả final structured summary.

### 6.3 Review contract

Codex review trả:

```json
{
  "verdict": "PASS",
  "findings": [],
  "retryInstruction": null,
  "scopeChange": null
}
```

`verdict` chỉ nhận `PASS`, `RETRY` hoặc `BLOCKED`. `RETRY` phải có instruction
cụ thể và dựa trên evidence. `BLOCKED` phải có ít nhất một finding giải thích
điều kiện không thể tự giải quyết. Scope change chỉ được dùng để replan task
chưa tích hợp; nó không hợp thức hóa thay đổi ngoài scope đã xảy ra.

## 7. State machine

Run state:

```text
PREPARING -> PLANNING -> SCHEDULING -> EXECUTING -> INTEGRATING
                              ^             |             |
                              +-------------+-------------+
                                            |
                              COMPLETED | BLOCKED | PAUSED
```

Mỗi task có state `PENDING`, `READY`, `RUNNING`, `VERIFYING`, `REVIEWING`,
`READY_TO_INTEGRATE`, `INTEGRATING`, `RETRYING`, `COMPLETED`, `BLOCKED` hoặc
`INTEGRATION_BLOCKED`. Một task có attempt `1/3` ở lần chạy đầu và tối đa `3/3`
sau hai retry.

State được ghi trước và sau mọi side effect quan trọng. Khi resume từ một bước
không có completion evidence, Controller kiểm tra worktree, patch artifact và
working tree chính trước khi chạy lại bước idempotent tương ứng.

Nếu một worker bị BLOCKED, các worker độc lập đang chạy được phép đi đến safe
boundary và lưu kết quả. Scheduler không chạy task phụ thuộc vào task bị chặn.
Run chuyển BLOCKED sau khi không còn worker có thể tiến triển an toàn.

## 8. Định dạng process.md

Phần người đọc có dạng:

```markdown
# Codex–Claude Process

Run: 20260823-143000
Goal: Thêm chức năng đăng nhập
Status: EXECUTING
Active tasks: T02, T03
Updated: 2026-08-23T07:42:15Z

| Task | Worker | Status | Attempt | Worktree | Context |
|------|--------|--------|---------|----------|---------|
| T01 | W01 | COMPLETED | 1/3 | cleaned | 38.2% |
| T02 | W01 | RUNNING | 1/3 | run/T02-worker | 31.4% |
| T03 | W02 | RUNNING | 1/3 | run/T03-worker | 44.8% |

## Current activity

Hai worker đang thực hiện T02 và T03.

## Last integration

T01.patch đã áp dụng thành công.
```

JSON state block chứa tối thiểu:

- `schemaVersion`, `runId`, `repository`, `initialHead`;
- goal, run status, phase và timestamps UTC;
- Codex thread ID và scheduler configuration;
- task DAG, worker assignment và active task list;
- attempt, Claude session history và context usage theo worker;
- worktree path, patch artifact, patch digest và integration status;
- verification command/result đã lọc;
- blocker hoặc pause reason;
- policy, resource locks và timeout áp dụng cho run.

`resume` từ chối nếu JSON sai schema/version, repository không khớp, initial
HEAD đã thay đổi, accepted patch không khớp digest hoặc Controller khác giữ lock.

## 9. Claude context rotation

Ngưỡng mặc định là 55%, cấu hình bằng `--claude-context-limit`. Context dùng
được tính từ usage của response gần nhất, không dùng tổng token cộng dồn:

```text
used_tokens = input_tokens
            + cache_creation_input_tokens
            + cache_read_input_tokens

used_percent = used_tokens / context_window * 100
```

`context_window` lấy từ `modelUsage.<model>.contextWindow` của Claude result và
được cache theo model. Nếu window size chưa có trong invocation đầu, Controller
áp dụng rotation tại result boundary đầu tiên. Controller không kill Claude giữa
file edit hoặc command; ngưỡng được thực thi tại safe response/result boundary.

Khi đạt ngưỡng, worker không resume session cũ. Controller tạo handoff gồm task
contract, file đổi, diff summary, test result và việc còn lại, rồi mở session mới
trong cùng isolated worktree. Rotation không tăng task attempt. Task bị BLOCKED
nếu cần hơn ba lần rotation.

Mỗi task mới luôn bắt đầu một Claude session mới. Context và rotation của các
worker hoàn toàn độc lập.

## 10. Không xung đột và tích hợp patch

Phòng tránh conflict có ba lớp:

1. Codex khai báo dependency, write scope và resource lock.
2. Scheduler tự tính overlap và chỉ xếp task hợp lệ vào cùng wave.
3. PatchIntegrator kiểm tra actual changed paths và `git apply --check` trước
   khi thay đổi working tree chính.

Patch của cùng wave được apply theo task ID ổn định để kết quả tái lập. Nếu actual
path vượt scope, hai patch chạm cùng path hoặc `git apply --check` thất bại,
Controller không cố merge và không hỏi AI sửa trực tiếp trên main tree. Task
chuyển `INTEGRATION_BLOCKED`; evidence được ghi vào `process.md` để người dùng
quyết định replan hay xử lý thủ công.

Task wave sau nhận toàn bộ accepted patch của wave trước. Các worker cùng wave
không thấy patch của nhau vì chúng phải độc lập theo contract.

## 11. Quyền hạn và bảo mật

- Codex luôn read-only.
- Claude dùng `--permission-mode dontAsk` cùng tool set tối thiểu.
- Không cấp rule rộng như `Bash(*)`.
- Claude chỉ được đọc/sửa trong worker worktree và write scope của task.
- Git command của Claude chỉ đọc: `status`, `diff`, `log`, `show`, `rev-parse`.
- Cấm `git commit`, `push`, `reset`, `clean`, `checkout`, `stash`, `worktree` và
  thay đổi history/ref. Chỉ Controller quản lý worktree và patch.
- Cấm publish, deploy, destructive filesystem/database command, thay đổi
  credential và đọc secret.
- `process.md` do Controller sở hữu và không nằm trong worker worktree.
- Authentication ưu tiên session đăng nhập sẵn của Codex và Claude CLI.
- Verification subprocess nhận environment allowlist, không nhận API key,
  access token hoặc biến môi trường nhạy cảm.
- Prompt/log redaction che giá trị từ tên biến phổ biến như token, password,
  secret, API key, authorization và cookie.

Tool không tuyên bố tạo sandbox OS hoàn chỉnh cho arbitrary repository code.
Chạy test/build của repository vẫn thực thi code cục bộ; tài liệu sử dụng phải
nêu rõ trust boundary này.

## 12. Timeout, retry và lỗi

Timeout mặc định:

- Codex plan/review: 10 phút;
- Claude task: 30 phút;
- mỗi verification command: 15 phút.

| Loại lỗi | Hành vi |
|---|---|
| Thiếu CLI, chưa đăng nhập, repo dirty | `BLOCKED` trước khi sửa file |
| Task DAG có cycle hoặc scope không hợp lệ | Từ chối plan và yêu cầu Codex lập lại một lần |
| Rate limit, lỗi mạng, provider overload | Infra retry với exponential backoff; không tăng task attempt |
| Claude làm sai hoặc verification fail | Codex review, sau đó dùng tối đa hai task retry |
| JSON/JSONL sai contract | Chạy lại agent subprocess một lần; sai tiếp thì `BLOCKED` |
| Context đạt ngưỡng | Rotate session; không tăng task attempt |
| Quá ba rotation trong task | `BLOCKED` |
| Scope hoặc patch conflict | Không apply; `INTEGRATION_BLOCKED` |
| Verification timeout | Dừng process tree, lưu evidence và gửi Codex review |
| Ctrl+C | Dừng mềm worker, giữ worktree, ghi `PAUSED`, sau đó thoát |
| Máy/process chết | Atomic state giữ checkpoint; `resume` kiểm tra artifact rồi tiếp tục |

SIGINT/CTRL_BREAK được ưu tiên để dừng mềm. Sau grace period, Controller mới
terminate process tree và ghi rõ việc dừng có thể để lại turn chưa hoàn tất.

## 13. Hoàn tất run

Sau mỗi wave, accepted patch đã được apply tuần tự vào working tree chính. Sau
task cuối:

1. chạy `finalVerificationCommands` trên working tree chính;
2. lấy full diff từ initial HEAD, loại `process.md` và artifact nội bộ;
3. yêu cầu Codex review tổng thể lần cuối;
4. chỉ ghi `COMPLETED` nếu verification thành công và final review PASS;
5. giữ toàn bộ source change chưa commit cho người dùng kiểm tra;
6. dọn worktree và artifact tạm do tool sở hữu sau khi state cuối đã ghi xong.

Nếu final review RETRY, Codex phải chỉ ra task sở hữu thay đổi cần sửa; retry vẫn
tuân theo attempt budget. Nếu không thể quy trách nhiệm an toàn, run BLOCKED.

## 14. Cấu trúc package

```text
codex-claude/
├── pyproject.toml
├── src/codex_claude/
│   ├── cli.py
│   ├── controller.py
│   ├── scheduler.py
│   ├── state.py
│   ├── process_store.py
│   ├── codex_adapter.py
│   ├── claude_adapter.py
│   ├── worktree_manager.py
│   ├── patch_integrator.py
│   ├── git_inspector.py
│   ├── verifier.py
│   ├── security.py
│   └── schemas/
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/
```

Runtime ưu tiên Python standard library: `argparse`, `asyncio`, `json`,
`dataclasses`, `enum`, `pathlib`, `tempfile` và `subprocess`. `pytest`, Ruff và
mypy là development dependencies.

## 15. Chiến lược kiểm thử

### 15.1 Unit test

- state transition và invariant;
- DAG validation, ready queue và deterministic scheduling;
- write scope overlap và resource locks;
- task retry và infra retry;
- context calculation và session rotation theo worker;
- Codex/Claude JSONL parser;
- contract validation, prompt construction và redaction;
- command policy và argv validation;
- atomic render/parse `process.md`;
- patch digest, actual-path check và integration order.

### 15.2 Integration test

Test tạo Git repository tạm và fake `codex`/`claude` executable. Fake CLI phát
JSONL, sửa file và trả exit code có kiểm soát. Test mặc định không gọi AI thật,
không cần credential và không tốn token.

Các scenario bắt buộc:

1. hai task độc lập chạy đồng thời trong hai worktree và tích hợp thành công;
2. hai write scope overlap bị scheduler chuyển thành chạy tuần tự;
3. task dependency chỉ chạy sau khi patch dependency đã tích hợp;
4. resource lock giống nhau ngăn verification chạy đồng thời;
5. unexpected changed path hoặc patch conflict không được apply;
6. verification fail, Codex yêu cầu retry, Claude sửa thành công;
7. context đạt 55% và session mới tiếp tục trong cùng worktree;
8. ba rotation dẫn tới `BLOCKED`;
9. Ctrl+C tạo `PAUSED`, giữ worktree và resume thành công;
10. JSONL hỏng, timeout và provider overload;
11. repo dirty, HEAD đổi và hai Controller chạy đồng thời;
12. secret không xuất hiện trong prompt summary hoặc `process.md`;
13. tương thích Windows và Linux.

Smoke test với Codex/Claude thật là opt-in và chỉ chạy khi người dùng đã đăng
nhập cả hai CLI.

Quality gate:

```text
ruff check .
ruff format --check .
mypy src
pytest
```

## 16. Tiêu chí chấp nhận MVP

MVP đạt khi:

- `run` nhận goal và repository sạch, tạo task DAG rồi thực hiện an toàn;
- mặc định tối đa hai Claude worker chạy đồng thời khi scope/lock độc lập;
- mỗi worker được cách ly trong detached worktree;
- chỉ Claude tạo source diff và Codex chỉ review read-only;
- patch chỉ được apply sau actual-scope check, verification, Codex PASS và
  `git apply --check`;
- `process.md` hiển thị mọi active worker/task và resume được sau gián đoạn;
- task retry, infra retry, timeout và blocker tuân đúng policy;
- Claude session không được resume sau khi đạt ngưỡng context 55%;
- tool không commit/push và từ chối command ngoài policy;
- final verification và Codex final review quyết định `COMPLETED`;
- unit/integration quality gate chạy thành công trên Windows và Linux.

## 17. Tài liệu tham khảo

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)
- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)
- [Claude context window and usage](https://code.claude.com/docs/en/statusline)
- [Claude Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python)
