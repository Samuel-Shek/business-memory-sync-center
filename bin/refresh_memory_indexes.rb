#!/usr/bin/env ruby
# frozen_string_literal: true

require_relative "archive_guard"
ArchiveGuard.require_opt_in!

require "date"
require "fileutils"
require "json"
require "open3"
require "optparse"
require "pathname"
require "securerandom"
require "sqlite3"
require "time"
require "yaml"

BRIDGE_ROOT = Pathname.new(File.expand_path("..", __dir__))
CONFIG_PATH = BRIDGE_ROOT.join("config.json")
CONFIG = JSON.parse(CONFIG_PATH.read(encoding: "UTF-8"))
ROOT = Pathname.new(CONFIG.fetch("memory_root")).expand_path
WATCH_NOTES_ROOT = Pathname.new(CONFIG.fetch("watch_notes_root")).expand_path
LOCK_PATH = BRIDGE_ROOT.join("state/memory-index.lock")
DB_PATH = BRIDGE_ROOT.join("state/bridge.sqlite")
DINGTALK_DB_PATH = Pathname.new(CONFIG.fetch("external_root")).join("dingtalk-chat/state/chat.sqlite")
WATCH_HEALTH_PATH = BRIDGE_ROOT.join("state/folder_watcher_health.json")
OVERVIEW_PATH = ROOT.join("00_记忆总览.md")
STATUS_PATH = ROOT.join("99_规则与模板/系统状态.md")
CATEGORY_DIRS = %w[01_关键业务认知 02_内部业务资料 03_外部沟通 04_对话来源摘要 90_待确认].freeze
WIN_ID_PREFIX = "BUSINESS-WIN-"
WINDOWS_QUICK_CAPTURE = "windows_quick_capture"
SEMANTIC_PROCESSED = %w[manual_merged merged no_business_change tombstone_recorded].freeze
OVERVIEW_BEGIN = "<!-- WIN11-INCOMING-SUMMARY:START -->"
OVERVIEW_END = "<!-- WIN11-INCOMING-SUMMARY:END -->"
INDEX_BUDGETS = CONFIG.fetch("index_budgets")
CHARS_PER_TOKEN = Float(INDEX_BUDGETS.fetch("chars_per_token"))
AI_SOFT_CHARS = (Integer(INDEX_BUDGETS.fetch("ai_soft_tokens")) * CHARS_PER_TOKEN).floor
AI_HARD_CHARS = (Integer(INDEX_BUDGETS.fetch("ai_hard_tokens")) * CHARS_PER_TOKEN).floor
PENDING_HARD_CHARS = (Integer(INDEX_BUDGETS.fetch("pending_hard_tokens")) * CHARS_PER_TOKEN).floor
BUSINESS_ROUTER = BRIDGE_ROOT.join("bin/business_page_router.py")

def atomic_write(path, content)
  path = Pathname.new(path)
  mode = path.exist? ? path.stat.mode & 0o777 : 0o644
  temporary = path.dirname.join(".#{path.basename}.tmp-#{Process.pid}-#{SecureRandom.hex(4)}")
  File.open(temporary, "wb", mode) do |file|
    file.write(content)
    file.flush
    file.fsync
  end
  File.chmod(mode, temporary)
  File.rename(temporary, path)
  File.open(path.dirname.to_s, "r") { |directory| directory.fsync }
ensure
  File.unlink(temporary) if defined?(temporary) && temporary && File.exist?(temporary)
end

def with_index_lock
  if ENV["BUSINESS_MEMORY_INDEX_LOCK_HELD"] == "1"
    yield
    return
  end
  FileUtils.mkdir_p(LOCK_PATH.dirname)
  File.open(LOCK_PATH, File::RDWR | File::CREAT, 0o600) do |lock|
    File.chmod(0o600, LOCK_PATH)
    lock.flock(File::LOCK_EX)
    yield
  end
end

def open_query_only_database(path)
  database = SQLite3::Database.new(path.to_s)
  database.busy_timeout(5000)
  database.execute("PRAGMA query_only=ON")
  database
rescue StandardError
  database&.close
  raise
end

def substantive_summary(body)
  preferred = %w[记忆内容 当前状态 开放问题 判断 业务意义 对话摘要]
  sections = preferred.map do |heading|
    body[/^##\s+#{Regexp.escape(heading)}\s*$\n+(.*?)(?=^##\s|\z)/m, 1]
  end.compact
  sections << body
  candidate = nil
  sections.each do |section|
    paragraphs = section.split(/\n\s*\n/).map do |paragraph|
      lines = paragraph.lines.map(&:strip).reject do |line|
        line.empty? || line.start_with?("#", "<!--", "-->") ||
          line.match?(/\A(?:模板|填写|说明|来源|边界|注意)[：:]/) ||
          line.match?(/\A[-*]\s*(?:待填写|无|N\/A|TODO)\b/i) ||
          line.match?(/\A\|?\s*(?:---|ID\b)/i)
      end
      lines.join(" ")
    end.reject(&:empty?)
    candidate = paragraphs.find { |paragraph| paragraph.match?(/[\p{Han}A-Za-z0-9]/) }
    break if candidate
  end
  cleaned = candidate.to_s
    .gsub(/!\[[^\]]*\]\([^)]*\)/, "")
    .gsub(/\[([^\]]+)\]\([^)]*\)/, '\1')
    .gsub(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/) { Regexp.last_match(2) || Regexp.last_match(1) }
    .gsub(/[`*_>#]/, "")
    .gsub(/\A[-+]\s+/, "")
    .gsub(/\s+/, " ").strip
  cleaned = "（原条目待核对）" if cleaned.empty?
  cleaned.length > 50 ? "#{cleaned[0, 49]}…" : cleaned
end

def metadata(path)
  text = path.read(encoding: "UTF-8")
  match = text.match(/\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\z)(.*)\z/m)
  return nil unless match
  data = YAML.safe_load(match[1], permitted_classes: [Date, Time], aliases: false) || {}
  return nil unless data["memory_id"]

  data.merge("path" => path, "summary" => substantive_summary(match[2]))
end

def display_id(memory_id)
  memory_id.to_s.sub("BUSINESS-20260824-", "").sub("BUSINESS-", "")
end

def link_for(data)
  relative = data.fetch("path").relative_path_from(ROOT).to_s.sub(/\.md\z/, "")
  "[[#{relative}]]"
end

def table_for(items)
  lines = ["| ID | 摘要 | 条目 |", "| --- | --- | --- |"]
  items.each do |data|
    lines << "| #{display_id(data.fetch('memory_id'))} | #{data.fetch('summary').gsub('|', '／')} | #{link_for(data)} |"
  end
  lines.join("\n")
end

def section_for(title, items, preface: nil)
  return "" if items.empty?
  lines = ["## #{title}", ""]
  lines.concat([preface, ""]) if preface && !preface.empty?
  lines << table_for(items)
  lines.join("\n")
end

def iso_value(value)
  return Time.at(0) if value.nil?
  Time.parse(value.to_s)
rescue ArgumentError
  Time.at(0)
end

def latest_incoming_event
  candidates = []
  if DB_PATH.file? && !DB_PATH.symlink?
    database = open_query_only_database(DB_PATH)
    tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").flatten
    candidates << database.get_first_value("SELECT MAX(processed_at) FROM events") if tables.include?("events")
    if tables.include?("source_events")
      candidates << database.get_first_value(
        "SELECT MAX(processed_at) FROM source_events WHERE status NOT LIKE 'capture_error:%'"
      )
    end
    database.close
    database = nil
  end
  if DINGTALK_DB_PATH.file? && !DINGTALK_DB_PATH.symlink?
    database = open_query_only_database(DINGTALK_DB_PATH)
    tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").flatten
    if tables.include?("semantic_batches")
      candidates << database.get_first_value(
        "SELECT MAX(completed_at) FROM semantic_batches WHERE status='applied'"
      )
    end
  end
  candidates.compact.map(&:to_s).reject(&:empty?).max
ensure
  database&.close
end

def watcher_full_scan_at
  return nil unless WATCH_HEALTH_PATH.file? && !WATCH_HEALTH_PATH.symlink?
  JSON.parse(WATCH_HEALTH_PATH.read(encoding: "UTF-8"))["last_successful_full_scan_at"]
rescue JSON::ParserError
  nil
end

def bridge_index_snapshot
  values = {
    "index_dirty_generation" => nil,
    "index_applied_generation" => nil,
    "index_last_refreshed_at" => nil,
    "index_last_error" => nil
  }
  return values.merge("index_state" => "not_initialized") unless DB_PATH.file? && !DB_PATH.symlink?

  database = open_query_only_database(DB_PATH)
  tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").flatten
  return values.merge("index_state" => "not_initialized") unless tables.include?("maintenance_state")

  keys = values.keys
  placeholders = keys.map { "?" }.join(",")
  database.execute(
    "SELECT key, value FROM maintenance_state WHERE key IN (#{placeholders})",
    *keys
  ).each do |key, value|
    values[key] = value
  end
  dirty = Integer(values["index_dirty_generation"].to_s, 10)
  applied = Integer(values["index_applied_generation"].to_s, 10)
  last_error = values["index_last_error"].to_s
  state = if !last_error.empty?
            "error"
          elsif dirty == applied
            "up_to_date"
          else
            "stale"
          end
  values.merge(
    "index_dirty_generation" => dirty,
    "index_applied_generation" => applied,
    "index_last_error" => last_error,
    "index_state" => state
  )
rescue ArgumentError, TypeError
  values.merge("index_state" => "not_initialized")
ensure
  database&.close
end

def parse_overview_committed_snapshot(text)
  values = %w[generated_at last_event_ingested_at last_successful_full_scan_at].to_h do |key|
    [key, text[/^#{Regexp.escape(key)}:\s*(\S+)\s*$/, 1]]
  end
  raise "overview generated_at is missing" if values["generated_at"].to_s.empty?

  snapshot = { "generated_at" => Time.iso8601(values.fetch("generated_at")).iso8601 }
  %w[last_event_ingested_at last_successful_full_scan_at].each do |key|
    value = values[key]
    if value.to_s.empty? || value == "null"
      snapshot[key] = nil
    else
      snapshot[key] = Time.iso8601(value).iso8601
    end
  end
  snapshot
rescue ArgumentError
  raise "overview committed snapshot contains an invalid timestamp"
end

def overview_committed_snapshot
  raise "missing overview for committed generation" unless OVERVIEW_PATH.file? && !OVERVIEW_PATH.symlink?

  parse_overview_committed_snapshot(OVERVIEW_PATH.read(encoding: "UTF-8"))
end

def update_overview(formal_pending_count, conversation_pending_count, quick_count,
                    external_count, external_pending_count, timestamp,
                    last_event_ingested_at, last_successful_full_scan_at)
  text = OVERVIEW_PATH.read(encoding: "UTF-8")
  marker = /#{Regexp.escape(OVERVIEW_BEGIN)}.*?#{Regexp.escape(OVERVIEW_END)}/m
  raise "missing Win11 incoming summary markers in #{OVERVIEW_PATH}" unless text.match?(marker)

  event_label = last_event_ingested_at.to_s.empty? ? "尚无成功事件" : last_event_ingested_at
  scan_label = last_successful_full_scan_at.to_s.empty? ? "尚无成功完整扫描" : last_successful_full_scan_at
  replacement = <<~MD.strip
    #{OVERVIEW_BEGIN}
    - 原位核验说明 #{formal_pending_count} 条；对话来源待核 #{conversation_pending_count} 条、快捷投递异常 #{quick_count} 条；外置机器来源 #{external_count} 条（待自动整理 #{external_pending_count} 条）。入口见 [[90_待确认/00_待确认总表]]，正文均在对应业务页面，机器来源按 UUID 从来源索引找回。
    - 最近事件入库：#{event_label}；最近成功完整扫描：#{scan_label}。
    #{OVERVIEW_END}
  MD
  updated = text.sub(marker, replacement)
  if updated.match?(/^generated_at:/)
    updated = updated.sub(/^generated_at: .*$/, "generated_at: #{timestamp}")
  else
    updated = updated.sub(/^updated_at: .*$/, "generated_at: #{timestamp}\ncontent_reviewed_at: null")
  end
  unless updated.match?(/^content_reviewed_at:/)
    updated = updated.sub(/^generated_at: .*$/, "\\0\ncontent_reviewed_at: null")
  end
  # These values answer different questions and must remain machine-readable:
  # generation time is not content review, event ingestion is not source scan.
  updated = updated.gsub(/^last_(?:event_ingested|successful_full_scan)_at:.*\n?/, "")
  event_value = last_event_ingested_at.to_s.empty? ? "null" : last_event_ingested_at
  scan_value = last_successful_full_scan_at.to_s.empty? ? "null" : last_successful_full_scan_at
  updated = updated.sub(
    /^content_reviewed_at: .*$/,
    "\\0\nlast_event_ingested_at: #{event_value}\nlast_successful_full_scan_at: #{scan_value}"
  )
  atomic_write(OVERVIEW_PATH, updated)
end

def render_system_status(notes_count:, formal_count:, formal_current_count:,
                         conversation_pending_count:, quick_count:, business_claim_count:,
                         external_count:, external_pending_count:, timestamp:,
                         last_event_ingested_at:, last_successful_full_scan_at:, index_snapshot:)
  event_value = last_event_ingested_at.to_s.empty? ? "null" : last_event_ingested_at
  scan_value = last_successful_full_scan_at.to_s.empty? ? "null" : last_successful_full_scan_at
  refresh_value = index_snapshot["index_last_refreshed_at"].to_s.empty? ? "null" : index_snapshot["index_last_refreshed_at"]
  error_value = JSON.generate(index_snapshot["index_last_error"].to_s)
  event_label = last_event_ingested_at.to_s.empty? ? "尚无成功事件" : last_event_ingested_at
  scan_label = last_successful_full_scan_at.to_s.empty? ? "尚无成功完整扫描" : last_successful_full_scan_at
  error_label = index_snapshot["index_last_error"].to_s.empty? ? "无" : index_snapshot["index_last_error"]
  <<~MD
    ---
    document_type: memory_system_status
    version: 7.0
    generated_at: #{timestamp}
    content_reviewed_at: null
    snapshot_source: refresh_memory_indexes
    last_event_ingested_at: #{event_value}
    last_successful_full_scan_at: #{scan_value}
    index_dirty_generation: #{index_snapshot["index_dirty_generation"] || "null"}
    index_applied_generation: #{index_snapshot["index_applied_generation"] || "null"}
    index_last_refreshed_at: #{refresh_value}
    index_last_error: #{error_value}
    index_state: #{index_snapshot["index_state"]}
    memory_notes: #{notes_count}
    formal_memory_notes: #{formal_count}
    formal_current_notes: #{formal_current_count}
    conversation_pending_notes: #{conversation_pending_count}
    quick_capture_notes: #{quick_count}
    inline_pending_claims: #{business_claim_count}
    external_watch_notes: #{external_count}
    external_watch_pending_notes: #{external_pending_count}
    applies_to:
      - project:business-energy
    ---

    本页由业务记忆索引刷新链自动生成，不接受人工维护计数。

    ## 当前索引快照

    - 库内记忆 #{notes_count} 条，其中正式业务 #{formal_count} 条、当前可检索正式记录 #{formal_current_count} 条。
    - 原位核验说明 #{business_claim_count} 条；对话来源待核 #{conversation_pending_count} 条；快捷投递异常 #{quick_count} 条。
    - 外置机器来源 #{external_count} 条，其中待自动整理 #{external_pending_count} 条。
    - 最近事件入库：#{event_label}；最近成功完整扫描：#{scan_label}。

    ## 索引协调状态

    - 索引状态：`#{index_snapshot["index_state"]}`；代际：`#{index_snapshot["index_dirty_generation"] || "未知"}`／`#{index_snapshot["index_applied_generation"] || "未知"}`；最近刷新：#{refresh_value == "null" ? "未知" : refresh_value}。
    - 最近索引错误：#{error_label}。

    ## 证据边界

    - 本页是 `generated_at` 时点的索引快照，不等于实时服务健康、人工审核或自然事件验收。
    - 进入维护、抽取漂移来源或声明当前运行状态前，仍须运行完整校验并核对现场进程。
    - 历史实施与验收边界见 [[99_规则与模板/实施方案与验收/业务记忆库批次2.1验收回执_20260826|业务记忆库批次 2.1 验收回执]]。
  MD
end

def load_business_claims
  stdout, stderr, status = Open3.capture3(
    "/usr/bin/python3", BUSINESS_ROUTER.to_s, "--config", CONFIG_PATH.to_s, "list"
  )
  raise "business claim scan failed: #{stderr.to_s.strip}" unless status.success?
  payload = JSON.parse(stdout)
  claims = payload["claims"]
  raise "business claim scan returned an invalid payload" unless claims.is_a?(Array)
  claims
end

def write_business_claim_index
  stdout, stderr, status = Open3.capture3(
    "/usr/bin/python3", BUSINESS_ROUTER.to_s, "--config", CONFIG_PATH.to_s, "write-index"
  )
  raise "business claim index refresh failed: #{stderr.to_s.strip}" unless status.success?
  JSON.parse(stdout)
end

def render_ai_index(groups, selected_ids, total, timestamp, external_count, external_pending, pending_count)
  selected_groups = groups.map do |title, items, preface|
    [title, items.select { |item| selected_ids.include?(item.fetch("memory_id")) }, preface]
  end
  displayed = selected_groups.inject(0) { |sum, (_title, items, _preface)| sum + items.length }
  omitted = total - displayed
  sections = selected_groups.map do |title, items, preface|
    section_for(title, items, preface: preface)
  end.reject(&:empty?).join("\n\n")
  omitted_line = if omitted.positive?
                   "预算内展开 #{displayed} 条，省略 #{omitted} 条；省略项仍在原目录，可按 `memory_id` 搜索，未改变事实状态。"
                 else
                   "已展开全部 #{displayed} 条正式当前记录。"
                 end
  <<~MD
    ---
    document_type: internal_business_memory_index
    version: 4.0
    generated_at: #{timestamp}
    content_reviewed_at: null
    scope: project:business-energy
    authority: ai_retrieval_index
    displayed_records: #{displayed}
    omitted_records: #{omitted}
    ---

    本表只展开正式业务层；普通对话来源只计数，不归入治理边界。待确认／待核对 #{pending_count} 条见 [[90_待确认/00_待确认总表]]；外置机器来源 #{external_count} 条（待自动整理 #{external_pending} 条）见 [[02_内部业务资料/20260825-Win11业务记忆合并来源索引]]。

    #{omitted_line}

    #{sections}

    ## 使用边界

    - 客户与联系人明细继续以小满 CRM、当前受控客户工作簿和责任人确认为准。
    - `source_check: matched` 只表示摘要与所查来源相符；自动化不得把不确定来源提升为 `confirmed`。
    - `pending`、`conflicting`、`unverified` 和 `review_required` 不得对外当作公司结论。
    - 内容等价时执行 `no-op` 或只追加来源指针；待核说明、状态和非结论元数据可按稳定 ID 幂等更新；实质性纠错或新结论必须新建记录，并通过 `supersedes` 保留历史链。
  MD
end

def render_pending_index(all_pending, formal_pending, conversation_pending, quick_incoming, timestamp)
  sorted = all_pending.sort_by { |data| [iso_value(data["observed_at"]), data.fetch("memory_id")] }
  featured = ([sorted.first] + sorted.last(5)).compact.uniq { |data| data.fetch("memory_id") }
  omitted = all_pending.length - featured.length
  distribution = all_pending.group_by { |data| data["source_system"].to_s.empty? ? "(missing)" : data["source_system"] }
    .transform_values(&:length).sort.to_h
  distribution_rows = distribution.map { |source, count| "| `#{source}` | #{count} |" }.join("\n")
  oldest = sorted.first
  oldest_line = oldest ? "#{oldest['observed_at']} · `#{oldest['memory_id']}` · #{link_for(oldest)}" : "无"
  <<~MD
    ---
    document_type: pending_memory_index
    version: 3.0
    generated_at: #{timestamp}
    content_reviewed_at: null
    applies_to:
      - project:business-energy
    total_records: #{all_pending.length}
    displayed_records: #{featured.length}
    omitted_records: #{omitted}
    ---

    本页仅统计兼容层输入：正式独立待核 #{formal_pending.length} 条、对话来源待核 #{conversation_pending.length} 条、快捷投递异常 #{quick_incoming.length} 条。正式独立待核和快捷投递均应为 0；业务待核正文必须位于对应正式页面，机器来源留在外置层。

    ## 来源分布

    | 来源 | 数量 |
    | --- | ---: |
    #{distribution_rows}

    ## 最旧待处理项

    - #{oldest_line}

    ## 最近 5 项与最旧项

    #{table_for(featured)}

    预算内展示 #{featured.length} 条，省略 #{omitted} 条。`90_待确认/` 只保留自动导航索引；对话来源保留在 `04_对话来源摘要/`，无法路由的机器输入进入外部技术隔离区。
  MD
end

def index_self_test
  passed = 0
  check = lambda do |condition, label|
    raise "self-test failed: #{label}" unless condition
    passed += 1
  end
  query_only_database = open_query_only_database(":memory:")
  check.call(query_only_database.get_first_value("PRAGMA query_only").to_i == 1, "SQLite reader is query-only")
  query_only_database.close
  kinds = %w[guardrail decision source_index status open_question fact judgment]
  synthetic = 100.times.map do |index|
    {
      "memory_id" => format("BUSINESS-20990101-%03d", index + 1),
      "memory_kind" => kinds[index % kinds.length],
      "observed_at" => format("2099-01-%02dT00:00:00+08:00", (index % 28) + 1),
      "summary" => "合成记录#{index + 1}用于验证入口预算、确定性顺序、省略数量和按 UUID 找回方式。",
      "path" => ROOT.join("90_待确认", format("synthetic-%03d.md", index + 1))
    }
  end
  groups = [
    ["治理边界", synthetic.select { |item| item["memory_kind"] == "guardrail" }, nil],
    ["已确认决定", synthetic.select { |item| item["memory_kind"] == "decision" }, nil],
    ["来源索引", synthetic.select { |item| item["memory_kind"] == "source_index" }, nil],
    ["当前状态与开放问题", synthetic.select { |item| %w[status open_question].include?(item["memory_kind"]) }, nil],
    ["已确认事实与判断", synthetic.select { |item| %w[fact judgment].include?(item["memory_kind"]) }, nil]
  ]
  selected_ids = groups.flat_map { |_title, items, _preface| items }.map { |item| item["memory_id"] }
  timestamp = "2099-01-01T00:00:00+08:00"
  ai = render_ai_index(groups, selected_ids, synthetic.length, timestamp, 0, 0, 100)
  while ai.length > AI_SOFT_CHARS && selected_ids.length > 1
    selected_ids.pop
    ai = render_ai_index(groups, selected_ids, synthetic.length, timestamp, 0, 0, 100)
  end
  pending = render_pending_index(synthetic, synthetic, [], [], timestamp)
  status = render_system_status(
    notes_count: synthetic.length, formal_count: synthetic.length,
    formal_current_count: synthetic.length, conversation_pending_count: 0,
    quick_count: 0, business_claim_count: 100, external_count: 0,
    external_pending_count: 0, timestamp: timestamp,
    last_event_ingested_at: timestamp, last_successful_full_scan_at: timestamp,
    index_snapshot: {
      "index_dirty_generation" => 7, "index_applied_generation" => 7,
      "index_last_refreshed_at" => timestamp, "index_last_error" => "",
      "index_state" => "up_to_date"
    }
  )
  check.call(ai.length <= AI_HARD_CHARS, "AI index hard budget")
  check.call(ai.include?("省略") && ai.include?("memory_id"), "AI index declares omitted UUID lookup")
  check.call(pending.length <= PENDING_HARD_CHARS, "pending index hard budget")
  check.call(pending.include?("省略 94 条") && pending.include?("最近 5 项"), "pending index keeps bounded recent view")
  check.call(status.include?("version: 7.0") && status.include?("index_state: up_to_date"), "generated status contract")
  check.call(status.include?("快照，不等于实时服务健康") && status.include?("memory_notes: 100"), "generated status boundary and counts")
  overview_snapshot = parse_overview_committed_snapshot(<<~MD)
    generated_at: 2099-01-01T00:00:00+08:00
    last_event_ingested_at: 2098-12-31T23:58:00+08:00
    last_successful_full_scan_at: 2098-12-31T23:59:00+08:00
  MD
  check.call(
    overview_snapshot == {
      "generated_at" => timestamp,
      "last_event_ingested_at" => "2098-12-31T23:58:00+08:00",
      "last_successful_full_scan_at" => "2098-12-31T23:59:00+08:00"
    },
    "status-only phase reuses committed overview watermarks"
  )
  {
    "verdict" => "pass", "tests_passed" => passed, "ai_chars" => ai.length,
    "pending_chars" => pending.length, "status_chars" => status.length
  }
end

if ARGV == ["--self-test"]
  puts JSON.pretty_generate(index_self_test)
  exit 0
end

options = { defer_status: false, status_only: false, target_generation: nil }
parser = OptionParser.new do |option|
  option.on("--defer-status") { options[:defer_status] = true }
  option.on("--status-only") { options[:status_only] = true }
  option.on("--target-generation GENERATION", Integer) do |generation|
    raise OptionParser::InvalidArgument, "generation must be positive" unless generation.positive?
    options[:target_generation] = generation
  end
end
parser.parse!(ARGV)
raise OptionParser::InvalidArgument, "unexpected arguments: #{ARGV.join(' ')}" unless ARGV.empty?
if options[:defer_status] && options[:status_only]
  raise OptionParser::InvalidArgument, "--defer-status and --status-only are mutually exclusive"
end

with_index_lock do
  initial_snapshot = bridge_index_snapshot
  committed_overview = options[:status_only] ? overview_committed_snapshot : nil
  target_generation = options[:target_generation]
  if target_generation
    dirty_generation = initial_snapshot["index_dirty_generation"]
    applied_generation = initial_snapshot["index_applied_generation"]
    if options[:status_only]
      unless applied_generation == target_generation
        raise "committed applied generation #{applied_generation.inspect} does not match target #{target_generation}"
      end
    elsif dirty_generation != target_generation
      raise "dirty generation #{dirty_generation.inspect} does not match target #{target_generation}"
    end
  end
  notes = CATEGORY_DIRS.flat_map { |dir| Dir.glob(ROOT.join(dir, "**", "*.md")).map { |p| Pathname.new(p) } }
    .map { |path| metadata(path) }.compact
    .sort_by { |data| data.fetch("memory_id") }
  external_watch = Dir.glob(WATCH_NOTES_ROOT.join("Win11文件夹监控-*.md"))
    .map { |path| metadata(Pathname.new(path)) }.compact
    .sort_by { |data| data.fetch("memory_id") }

  formal_current = notes.select do |data|
    data["status"] == "active" && data["memory_scope"] != "conversation_source" &&
      %w[confirmed not_applicable].include?(data["fact_status"])
  end
  formal_pending = notes.select do |data|
    data["status"] == "review_required" && data["memory_scope"] != "conversation_source" &&
      !data["memory_id"].start_with?(WIN_ID_PREFIX)
  end
  conversation_pending = notes.select do |data|
    data["memory_scope"] == "conversation_source" && data["status"] == "review_required"
  end
  quick_incoming = notes.select do |data|
    data["source_system"] == WINDOWS_QUICK_CAPTURE && data["status"] == "review_required"
  end
  all_pending = (formal_pending + conversation_pending + quick_incoming).uniq { |data| data.fetch("memory_id") }
  external_pending = external_watch.reject { |data| SEMANTIC_PROCESSED.include?(data["semantic_status"]) }
  business_claims = load_business_claims

  groups = [
    ["治理边界", formal_current.select { |data| data["memory_kind"] == "guardrail" }, "适用边界，不是业务事实本身。"],
    ["已确认决定", formal_current.select { |data| data["memory_kind"] == "decision" }, nil],
    ["来源索引", formal_current.select { |data| data["memory_kind"] == "source_index" }, nil],
    ["当前状态与开放问题", formal_current.select { |data| %w[status open_question].include?(data["memory_kind"]) }, nil],
    ["已确认事实与判断", formal_current.select { |data| %w[fact judgment].include?(data["memory_kind"]) }, nil]
  ]
  timestamp = if options[:status_only]
                committed_overview.fetch("generated_at")
              else
                Time.now.getlocal("+08:00").iso8601
              end
  priority_items = groups.flat_map { |_title, items, _preface| items }
  selected_ids = priority_items.map { |item| item.fetch("memory_id") }
  detailed = render_ai_index(
    groups, selected_ids, formal_current.length, timestamp, external_watch.length,
    external_pending.length, business_claims.length
  )
  while detailed.length > AI_SOFT_CHARS && selected_ids.length > 1
    selected_ids.pop
    detailed = render_ai_index(
      groups, selected_ids, formal_current.length, timestamp, external_watch.length,
      external_pending.length, business_claims.length
    )
  end
  raise "AI index exceeds hard budget (#{detailed.length} > #{AI_HARD_CHARS} chars)" if detailed.length > AI_HARD_CHARS

  pending_result = { "status" => "not_written" }
  unless options[:status_only]
    atomic_write(ROOT.join("99_规则与模板/AI检索总表.md"), detailed)
    pending_result = write_business_claim_index
  end
  pending_path = ROOT.join("90_待确认/00_待确认总表.md")
  pending_chars = pending_path.file? ? pending_path.size : 0
  last_event_ingested_at = if options[:status_only]
                             committed_overview["last_event_ingested_at"]
                           else
                             latest_incoming_event
                           end
  last_successful_full_scan_at = if options[:status_only]
                                   committed_overview["last_successful_full_scan_at"]
                                 else
                                   watcher_full_scan_at
                                 end
  index_snapshot = bridge_index_snapshot
  status_written = false
  unless options[:defer_status]
    status = render_system_status(
      notes_count: notes.length,
      formal_count: notes.count { |data| data["memory_scope"] != "conversation_source" },
      formal_current_count: formal_current.length,
      conversation_pending_count: conversation_pending.length,
      quick_count: quick_incoming.length,
      business_claim_count: business_claims.length,
      external_count: external_watch.length,
      external_pending_count: external_pending.length,
      timestamp: timestamp,
      last_event_ingested_at: last_event_ingested_at,
      last_successful_full_scan_at: last_successful_full_scan_at,
      index_snapshot: index_snapshot
    )
    atomic_write(STATUS_PATH, status)
    status_written = true
  end
  unless options[:status_only]
    update_overview(
      business_claims.length, conversation_pending.length, quick_incoming.length,
      external_watch.length, external_pending.length, timestamp,
      last_event_ingested_at, last_successful_full_scan_at
    )
  end

  mode = if options[:status_only]
           "status_only"
         elsif options[:defer_status]
           "content_deferred_status"
         else
           "full"
         end
  puts "mode=#{mode} target_generation=#{target_generation || 'none'} " \
       "notes=#{notes.length} formal_current=#{formal_current.length} " \
       "formal_pending=#{formal_pending.length} conversation_pending=#{conversation_pending.length} " \
       "quick_incoming=#{quick_incoming.length} external_watch=#{external_watch.length} " \
       "business_claims=#{business_claims.length} external_pending=#{external_pending.length} " \
       "ai_chars=#{detailed.length} pending_chars=#{pending_chars} " \
       "status_written=#{status_written} status_index_state=#{index_snapshot['index_state']} " \
       "pending_index=#{pending_result['status']}"
end
