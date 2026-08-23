# Codex–Claude Orchestrator Implementation Plan

> Ngày lập: 2026-08-23
>
> Trạng thái: sẵn sàng triển khai sau khi người dùng yêu cầu
>
> Design source of truth: [2026-08-23-codex-claude-orchestrator-design.md](../specs/2026-08-23-codex-claude-orchestrator-design.md)

## 1. Kết quả cần đạt

Xây dựng CLI Python 3.11+ tên `codex-claude` có thể:

- nhận prompt trực tiếp hoặc SRS `.md`/`.txt`;
- dùng Codex CLI ở chế độ read-only để lập kế hoạch và review;
- dùng Claude Code CLI để sửa code trong detached Git worktree riêng;
- chạy tối đa hai task độc lập song song theo mặc định;
- rotate Claude session khi context đạt ngưỡng mặc định 55%;
- verify và tích hợp binary patch tuần tự mà không commit/push;
- lưu toàn bộ trạng thái có thể resume trong `process.md`;
- nhận yêu cầu bổ sung bằng `add` và replan tại safe boundary.

Runtime ưu tiên Python standard library. Dependency phát triển tối thiểu gồm
`pytest`, `pytest-cov`, `ruff`, `mypy` và `build`; không dùng SDK
Codex/Claude.

## 2. Nguyên tắc triển khai

1. Mỗi task bên dưới bắt đầu bằng test thất bại, sau đó mới viết implementation tối thiểu.
2. Không chuyển task kế tiếp khi test chuyên biệt của task hiện tại chưa qua.
3. Mọi subprocess dùng argv và `shell=False`.
4. Test mặc định dùng fake executable và temporary Git repository, không gọi AI thật.
5. State machine, scope policy và patch validation thuộc Controller; không tin output của agent.
6. Mọi thay đổi trạng thái bền vững phải đi qua `ProcessStore`.
7. CLI đang được xây dựng không được tự commit hoặc push repository mà nó điều phối.

## 3. Cấu trúc đích

```text
pyproject.toml
README.md
src/codex_claude/
├── __init__.py
├── __main__.py
├── cli.py
├── config.py
├── controller.py
├── errors.py
├── input_loader.py
├── requirements.py
├── state.py
├── process_store.py
├── locking.py
├── security.py
├── subprocess_runner.py
├── codex_adapter.py
├── claude_adapter.py
├── scheduler.py
├── git_inspector.py
├── repository_rules.py
├── worktree_manager.py
├── patch_integrator.py
├── verifier.py
├── rendering.py
└── schemas/
    ├── plan.schema.json
    ├── review.schema.json
    └── claude-result.schema.json
tests/
├── conftest.py
├── fixtures/
│   ├── fake_codex.py
│   └── fake_claude.py
├── unit/
└── integration/
```

## 4. Các task triển khai

### Task 01 — Scaffold package và quality gate

**Files**

- Tạo: `pyproject.toml`
- Tạo: `README.md`
- Tạo: `src/codex_claude/__init__.py`
- Tạo: `src/codex_claude/__main__.py`
- Tạo: `src/codex_claude/cli.py`
- Tạo: `src/codex_claude/errors.py`
- Tạo: `tests/test_package.py`

**Thực hiện**

1. Viết test xác nhận package import được, `python -m codex_claude --help` trả mã 0
   và console script có ba subcommand `run`, `add`, `resume`.
2. Khai báo build bằng setuptools, Python `>=3.11`, console entry point và package data
   cho JSON Schema.
3. Cấu hình Ruff, Mypy strict, Pytest và coverage trong `pyproject.toml`.
4. Tạo parser khung; handler chưa triển khai phải trả lỗi domain rõ ràng, không traceback.
5. Viết README tối thiểu mô tả mục tiêu và cảnh báo tool để lại source change chưa commit.

**Kiểm tra**

```powershell
python -m pytest tests/test_package.py
python -m ruff check .
python -m mypy src
```

### Task 02 — Domain model và contract validation

**Files**

- Tạo: `src/codex_claude/state.py`
- Tạo: `src/codex_claude/config.py`
- Tạo: `src/codex_claude/schemas/plan.schema.json`
- Tạo: `src/codex_claude/schemas/review.schema.json`
- Tạo: `src/codex_claude/schemas/claude-result.schema.json`
- Tạo: `tests/unit/test_state.py`
- Tạo: `tests/unit/test_contracts.py`

**Thực hiện**

1. Viết test cho enum run/task/revision, transition hợp lệ và transition bị cấm.
2. Tạo dataclass typed cho `RunState`, `TaskState`, `WorkerState`,
   `RequirementRevision`, `VerificationResult`, `PatchRecord` và checkpoint.
3. Thêm `schemaVersion`, `generation`, timestamps UTC ISO-8601 và serializer
   deterministic.
4. Viết parser strict cho plan/review/Claude result: từ chối field thiếu, ID trùng,
   verdict lạ, command dạng chuỗi và kiểu dữ liệu sai.
5. Đóng gói schema đúng contract trong design spec; parser nội bộ tiếp tục kiểm tra các
   invariant mà JSON Schema không biểu diễn được.
6. Test round-trip state và forward-version rejection với lỗi recovery rõ ràng.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_state.py tests/unit/test_contracts.py
```

### Task 03 — Input, SRS và requirement revision append-only

**Files**

- Tạo: `src/codex_claude/input_loader.py`
- Tạo: `src/codex_claude/requirements.py`
- Tạo: `tests/unit/test_input_loader.py`
- Tạo: `tests/unit/test_requirements.py`

**Thực hiện**

1. Test mutual exclusion giữa positional prompt và `--spec`.
2. Test SRS chỉ nhận regular file `.md`/`.txt`, UTF-8 hợp lệ, tối đa 1 MiB;
   từ chối symlink bất ngờ, directory, encoding lỗi và file đổi digest.
3. Chuẩn hóa newline, tính SHA-256 trên bytes canonical và sinh summary đã giới hạn.
4. Ghi nguyên nội dung revision vào
   `.git/codex-claude/<run-id>/inputs/<revision-id>.<ext>` bằng atomic replace.
5. Giữ revision append-only với trạng thái `QUEUED`, `PLANNING`, `APPLIED`,
   `BLOCKED`; không cho sửa nội dung/digest revision đã tạo.
6. Test load lại artifact đủ để replan sau khi process `add` đã thoát.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_input_loader.py tests/unit/test_requirements.py
```

### Task 04 — Git inspection và chuẩn hóa write scope

**Files**

- Tạo: `src/codex_claude/git_inspector.py`
- Tạo: `src/codex_claude/repository_rules.py`
- Mở rộng: `src/codex_claude/security.py`
- Tạo: `tests/unit/test_git_inspector.py`
- Tạo: `tests/unit/test_repository_rules.py`
- Tạo: `tests/unit/test_scopes.py`

**Thực hiện**

1. Tạo fixture temporary Git repo có commit nền.
2. Test nhận diện repository root, HEAD, clean/dirty state, tracked `process.md`,
   untracked file và thay đổi HEAD.
3. Resolve Git common directory bằng Git plumbing thay vì giả định `.git` luôn là
   directory; mọi input/patch/archive artifact dùng đường dẫn đã resolve này.
4. Định nghĩa digest working tree deterministic từ status + binary diff + untracked
   content, loại trừ đúng artifact do tool sở hữu.
5. Chuẩn hóa path bằng POSIX repository-relative form; từ chối absolute path, drive,
   `..`, empty segment và glob.
6. Implement quan hệ file/directory scope và overlap theo path segment.
7. Định nghĩa exclusive global path mặc định: lockfile, migration/schema database,
   root build/config và package-manager metadata.
8. Phát hiện instruction file áp dụng theo hierarchy repository, tối thiểu
   `AGENTS.md` và `CLAUDE.md`; ghi digest và chỉ chuyển phần liên quan vào planning
   hoặc task handoff. Mâu thuẫn với security policy phải `BLOCKED`, không được nới quyền.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_git_inspector.py tests/unit/test_repository_rules.py tests/unit/test_scopes.py
```

### Task 05 — ProcessStore, Markdown state và locking

**Files**

- Tạo: `src/codex_claude/process_store.py`
- Tạo: `src/codex_claude/locking.py`
- Tạo: `src/codex_claude/rendering.py`
- Tạo: `tests/unit/test_process_store.py`
- Tạo: `tests/integration/test_process_locking.py`

**Thực hiện**

1. Test render `process.md` gồm phần Markdown dễ đọc và JSON machine block có marker.
2. Test parser chỉ tin đúng marker/schema; từ chối file người dùng, file tracked,
   JSON hỏng và schema version mới hơn.
3. Ghi state qua file tạm cùng filesystem + flush/fsync phù hợp + `os.replace`.
4. Thêm chính xác `/process.md` vào `.git/info/exclude` mà không xóa nội dung sẵn có.
5. Archive bản cuối vào `.git/codex-claude/<run-id>/process.final.md` trước run mới.
6. Implement repository ownership lock dài hạn và state-write lock ngắn hạn trên
   Windows/POSIX bằng standard library.
7. Implement compare-and-swap theo `generation`; integration test hai writer chứng minh
   revision từ `add` không bị Controller ghi đè.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_process_store.py tests/integration/test_process_locking.py
```

### Task 06 — Security policy và subprocess runner

**Files**

- Tạo: `src/codex_claude/security.py`
- Tạo: `src/codex_claude/subprocess_runner.py`
- Tạo: `tests/unit/test_security.py`
- Tạo: `tests/integration/test_subprocess_runner.py`
- Tạo: `tests/fixtures/emit_process_events.py`

**Thực hiện**

1. Test subprocess luôn nhận argv, fixed cwd, sanitized environment và không qua shell.
2. Tạo allowlist environment tối thiểu để tìm executable/chứng thực hợp lệ, đồng thời
   loại các biến nhạy cảm không cần chuyển cho worker.
3. Implement streaming stdout/stderr không deadlock, JSONL callback, output byte limit,
   redaction token/secret và lưu tail chẩn đoán.
4. Implement timeout/cancellation: process group trên POSIX và process-tree termination
   có PID cụ thể trên Windows; không để orphan worker.
5. Phân biệt executable-not-found, non-zero exit, malformed JSONL, timeout, cancellation
   và permission denial bằng typed error.
6. Tạo command-policy classifier dùng chung: chặn publish/deploy, Git mutation,
   destructive filesystem/database operation và credential command trước khi spawn.
7. Test child process, output lớn, partial line, invalid UTF-8 replacement và Ctrl+C.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_security.py tests/integration/test_subprocess_runner.py
```

### Task 07 — CodexAdapter read-only

**Files**

- Tạo: `src/codex_claude/codex_adapter.py`
- Tạo: `tests/fixtures/fake_codex.py`
- Tạo: `tests/unit/test_codex_adapter.py`
- Tạo: `tests/integration/test_codex_adapter_process.py`

**Thực hiện**

1. Fake CLI ghi lại argv/stdin và phát JSONL cho success, failure, malformed response,
   timeout và resume.
2. Tạo command plan dùng `codex exec --json`, sandbox read-only,
   `--output-schema` và file final output riêng.
3. Parse `thread.started`, turn/item/error event; lưu thread ID và final structured data.
4. Tạo review/replan qua `codex exec resume <thread-id>` với schema tương ứng.
5. Build prompt từ requirement, repository instruction và evidence đã truncate/redact;
   không chuyển raw Claude transcript.
6. Xác nhận adapter không cấp quyền ghi, không gọi commit/push và không nhận output ngoài schema.
7. Retry một lần khi JSON/JSONL sai contract; lỗi hạ tầng tạm thời dùng exponential
   backoff có giới hạn và không tiêu hao task attempt.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_codex_adapter.py tests/integration/test_codex_adapter_process.py
```

### Task 08 — ClaudeAdapter, usage và context rotation

**Files**

- Tạo: `src/codex_claude/claude_adapter.py`
- Tạo: `tests/fixtures/fake_claude.py`
- Tạo: `tests/unit/test_claude_adapter.py`
- Tạo: `tests/unit/test_context_rotation.py`
- Tạo: `tests/integration/test_claude_adapter_process.py`

**Thực hiện**

1. Fake CLI mô phỏng init/assistant/result, session ID, model usage, permission denial,
   context tăng dần và `--resume`.
2. Tạo argv `claude -p --output-format stream-json --verbose` với permission mode
   `dontAsk`, allowed/disallowed tools tối thiểu và structured result schema.
3. Build task handoff gồm objective, acceptance criteria, excerpts, dependency evidence,
   repository rules, write scope, commands và policy cấm.
4. Tính context percentage từ usage mới nhất và `contextWindow`; xử lý thiếu/không hợp
   lệ thành metric unavailable, không tự đoán.
5. Khi usage đạt ngưỡng, dừng tại message boundary, tạo handoff summary và mở session mới;
   tối đa ba rotation/task.
6. Retry trong cùng session khi còn an toàn; resume chỉ dùng đúng session ID/worktree/task.
7. Test Claude báo `scope_change_required` không được xem là quyền sửa ngoài scope.
8. Retry một lần khi structured output sai contract; lỗi hạ tầng tạm thời dùng
   exponential backoff có giới hạn và không tiêu hao task attempt.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_claude_adapter.py tests/unit/test_context_rotation.py tests/integration/test_claude_adapter_process.py
```

### Task 09 — Worktree và patch lifecycle

**Files**

- Tạo: `src/codex_claude/worktree_manager.py`
- Tạo: `src/codex_claude/patch_integrator.py`
- Tạo: `tests/integration/test_worktree_manager.py`
- Tạo: `tests/integration/test_patch_integrator.py`

**Thực hiện**

1. Test tạo detached worktree từ initial HEAD trong temp directory ngoài source tree.
2. Replay các accepted patch từ wave trước theo thứ tự digest đã lưu.
3. Thu tracked/deleted/untracked changes; chỉ dùng intent-to-add trong index worktree.
4. Tạo `git diff --binary`, SHA-256 và artifact
   `.git/codex-claude/<run-id>/patches/<task-id>.patch`.
5. So changed paths với normalized `writeScopes`; ngoài scope chuyển task sang blocked
   trước review/integration.
6. Trên main working tree, xác nhận HEAD/checkpoint, chạy `git apply --check`, sau đó
   apply tuần tự và cập nhật state.
7. Mô phỏng conflict, binary file, rename, delete và new file; conflict bất ngờ phải thành
   `INTEGRATION_BLOCKED`, patch/worktree vẫn còn để recovery.
8. Cleanup worktree chỉ sau khi patch đã tích hợp và state đã ghi thành công.

**Kiểm tra**

```powershell
python -m pytest tests/integration/test_worktree_manager.py tests/integration/test_patch_integrator.py
```

### Task 10 — Verifier an toàn

**Files**

- Tạo: `src/codex_claude/verifier.py`
- Tạo: `tests/unit/test_verifier.py`
- Tạo: `tests/integration/test_verifier_process.py`

**Thực hiện**

1. Validate command là non-empty argv, không chứa NUL, không phải shell string và phải
   qua command-policy classifier trước khi spawn.
2. Chạy command đúng worktree với timeout, sanitized environment và bounded output.
3. Ghi exit code, duration, timeout, redacted stdout/stderr digest và tail.
4. Không tự retry verification command có side effect; retry thuộc cấp task/controller.
5. Cho phép Controller giữ `resourceLocks` trong toàn thời gian verification.
6. Test pass, fail, timeout, missing executable, secret redaction và output truncation.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_verifier.py tests/integration/test_verifier_process.py
```

### Task 11 — DAG scheduler và parallel waves

**Files**

- Tạo: `src/codex_claude/scheduler.py`
- Tạo: `tests/unit/test_scheduler.py`
- Tạo: `tests/unit/test_scheduler_properties.py`

**Thực hiện**

1. Test ID trùng, missing dependency, self-dependency và cycle.
2. Tính dependency closure và chỉ chọn task có mọi dependency đã accepted.
3. Chọn wave deterministic theo plan order/ID, tối đa `maxWorkers`.
4. Không xếp chung task khi scope overlap, lock giao nhau, `parallelSafe=false` hoặc
   có global exclusive path.
5. Phân biệt pending, runnable, running, accepted, retryable, blocked và superseded.
6. Thêm property-style cases sinh nhiều DAG nhỏ để xác nhận một wave không bao giờ vi phạm
   dependency/scope/lock invariant.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_scheduler.py tests/unit/test_scheduler_properties.py
```

### Task 12 — Controller: run, wave, review và integration

**Files**

- Tạo: `src/codex_claude/controller.py`
- Mở rộng: `src/codex_claude/cli.py`
- Tạo: `tests/integration/test_controller_run.py`
- Tạo: `tests/integration/test_controller_parallel.py`
- Tạo: `tests/integration/test_controller_retry.py`

**Thực hiện**

1. Dựng end-to-end test với fake Codex/Claude và temporary Git repo.
2. Implement luồng `PREPARING -> PLANNING -> SCHEDULING -> EXECUTING -> INTEGRATING`.
3. Trước mọi side effect, ghi current activity/checkpoint vào `process.md`.
4. Chạy worker bằng `asyncio` với bounded concurrency; mỗi worker có worktree/session
   riêng và scheduler giữ lock tới khi verification kết thúc.
5. Thu patch/evidence, gọi Codex review và chỉ tích hợp verdict `PASS`.
6. Với `RETRY`, truyền evidence/retry instruction; giới hạn ba attempt tổng cộng.
7. Với permission/scope/security error hoặc exhausted retry, chuyển đúng trạng thái
   `BLOCKED`; không mở rộng quyền tự động.
8. Sau mỗi wave, kiểm tra main HEAD/working-tree checkpoint và chạy cross-task verification
   nếu plan yêu cầu.

**Kiểm tra**

```powershell
python -m pytest tests/integration/test_controller_run.py tests/integration/test_controller_parallel.py tests/integration/test_controller_retry.py
```

### Task 13 — add, replan, resume và crash recovery

**Files**

- Mở rộng: `src/codex_claude/controller.py`
- Mở rộng: `src/codex_claude/cli.py`
- Mở rộng: `src/codex_claude/process_store.py`
- Tạo: `tests/integration/test_add_revision.py`
- Tạo: `tests/integration/test_resume.py`
- Tạo: `tests/integration/test_crash_recovery.py`

**Thực hiện**

1. Test `add` lấy state-write lock, lưu artifact, append revision, tăng generation rồi thoát.
2. Controller đang chạy phát hiện generation mới tại safe boundary và chuyển
   `REPLAN_PENDING -> REPLANNING`; không đổi prompt worker giữa task.
3. Replan giữ immutable history của accepted task, chỉ supersede task chưa bắt đầu và giữ
   `requirementRefs`.
4. Cho phép `add` mở lại run `COMPLETED` khi HEAD/working-tree checkpoint khớp.
5. Với `PAUSED`/`BLOCKED`, lưu revision và áp dụng khi `resume`.
6. Resume đối chiếu SRS digest, HEAD, working-tree digest, patch digest, task attempt,
   Codex thread và Claude session trước khi chọn action idempotent tiếp theo.
7. Mô phỏng kill tại từng checkpoint quan trọng: sau tạo worktree, sau verify, sau ghi patch,
   sau apply patch nhưng trước state write; bảo đảm không apply cùng patch hai lần.
8. Ctrl+C dừng process tree, ghi `PAUSED` và để lại recovery artifacts.

**Kiểm tra**

```powershell
python -m pytest tests/integration/test_add_revision.py tests/integration/test_resume.py tests/integration/test_crash_recovery.py
```

### Task 14 — Preflight, process UX và final verification

**Files**

- Mở rộng: `src/codex_claude/cli.py`
- Mở rộng: `src/codex_claude/controller.py`
- Mở rộng: `src/codex_claude/rendering.py`
- Tạo: `tests/unit/test_cli.py`
- Tạo: `tests/integration/test_preflight.py`
- Tạo: `tests/integration/test_finalization.py`

**Thực hiện**

1. Validate option: positive timeout/worker, context limit 1–100, đúng input source.
2. Preflight Python/Git/Codex/Claude executable, repository, clean baseline, auth failure
   có thể nhận biết và process file ownership.
3. Render `process.md` hiển thị run/phase/current task/worker/context/attempt/revision
   sau mỗi state transition để người dùng chỉ cần mở file theo dõi.
4. Sau khi mọi task đã accepted, chạy `finalVerificationCommands` trên main working tree.
5. Gọi Codex final review với bounded evidence; chỉ chuyển `COMPLETED` khi verification
   và review đều PASS.
6. In summary cuối gồm changed paths, verification, blocked reason/recovery command và
   nhắc source chưa commit.
7. Đảm bảo mọi lỗi dự kiến trả exit code ổn định và không lộ secret/traceback mặc định.

**Kiểm tra**

```powershell
python -m pytest tests/unit/test_cli.py tests/integration/test_preflight.py tests/integration/test_finalization.py
```

### Task 15 — Full-system scenarios và tài liệu vận hành

**Files**

- Mở rộng: `README.md`
- Tạo: `docs/security-model.md`
- Tạo: `docs/recovery.md`
- Tạo: `tests/integration/test_full_system.py`
- Tạo: `tests/integration/test_windows_paths.py`
- Tạo: `tests/smoke/test_live_clis.py`
- Tạo: `.github/workflows/ci.yml`

**Thực hiện**

1. Full fake-system test cho prompt, SRS, hai task song song, scope conflict, retry,
   rotation 55%, queued `add`, crash/resume và final PASS.
2. Test Windows drive/path separator, filename Unicode và executable path có space.
3. Live smoke test phải opt-in bằng marker/environment flag, read-only fixture repo và
   không chạy trong CI mặc định.
4. CI matrix Windows + Linux với Python 3.11 trở lên; chạy lint, typecheck, unit,
   integration và coverage.
5. README ghi rõ cài đặt, command examples, `process.md`, working-tree requirement,
   context policy, permission policy và cách xử lý `BLOCKED`.
6. Security doc mô tả trust boundary, env forwarding, agent permissions, redaction,
   non-goals và cảnh báo prompt/SRS không thể mở rộng policy.
7. Recovery doc mô tả checkpoint validation, artifact location, resume refusal và
   cleanup worktree an toàn.

**Kiểm tra**

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m pytest -m "not live" --cov=codex_claude --cov-report=term-missing
python -m build
```

## 5. Ma trận acceptance cuối

| Yêu cầu | Test/chứng cứ chính |
| --- | --- |
| Prompt và SRS `.md`/`.txt` | `test_input_loader.py`, `test_full_system.py` |
| State trong `process.md`, không commit | `test_process_store.py`, kiểm tra Git integration |
| Codex read-only plan/review | `test_codex_adapter_process.py` |
| Claude là source editor duy nhất | adapter argv test + worktree diff test |
| Tối đa hai worker mặc định | `test_controller_parallel.py` |
| Không xung đột scope/lock | `test_scheduler.py`, `test_patch_integrator.py` |
| Rotate context tại 55% | `test_context_rotation.py`, full-system scenario |
| Tối đa 2 retry sau lần đầu | `test_controller_retry.py` |
| `add` append-only và safe-boundary replan | `test_add_revision.py` |
| Crash-safe resume | `test_crash_recovery.py`, `test_resume.py` |
| Không commit/push/deploy | fake argv audit + Git HEAD assertion |
| Không gọi AI thật trong test mặc định | marker `live` bị loại khỏi CI |

## 6. Thứ tự milestone

1. **Foundation:** Task 01–05 — package, contracts, input, Git và durable state.
2. **Agent boundary:** Task 06–10 — subprocess, adapters, worktree/patch và verifier.
3. **Orchestration:** Task 11–14 — scheduler, Controller, add/resume và finalization.
4. **Hardening:** Task 15 — cross-platform scenarios, docs và CI.

Mỗi milestone chỉ được xem là xong khi toàn bộ test của milestone và quality gate hiện có
đều qua. Implementation không bắt đầu cho đến khi người dùng yêu cầu triển khai.

## 7. Trạng thái triển khai

Cập nhật 2026-08-23:

- Task 01–14: hoàn tất implementation và kiểm thử trên Windows.
- Task 15: hoàn tất full-system hardening, tài liệu security/recovery, CI Windows/Linux,
  opt-in live smoke test và package build.
- Quality gate tại checkpoint: 41 test non-live pass, live smoke test được skip nếu chưa
  bật marker, Ruff check/format pass, Mypy strict pass và `python -m build` pass.
- Real CLI preflight: Codex và Claude capability/authentication đều pass.
- Source change đang để uncommitted; tool không tạo commit hoặc push.
