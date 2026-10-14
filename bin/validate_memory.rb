#!/usr/bin/env ruby
# frozen_string_literal: true

require_relative "archive_guard"
ArchiveGuard.require_opt_in!

Process.setpgid(0, 0) if ENV["BUSINESS_AUTOMATIC_FULL_VALIDATION"] == "1"

require "date"
require "base64"
require "digest"
require "find"
require "fileutils"
require "json"
require "open3"
require "pathname"
require "time"
require "tmpdir"
require "yaml"
require_relative "memory_validation_contracts"
require_relative "validation_progress"

begin
  require "sqlite3"
  SQLITE_AVAILABLE = true
rescue LoadError
  SQLITE_AVAILABLE = false
end

CATEGORY_DIRS = %w[
  01_关键业务认知 02_内部业务资料 03_外部沟通 04_对话来源摘要 90_待确认
].freeze
INDEX_RELATIVES = %w[
  00_记忆总览.md 99_规则与模板/AI检索总表.md 99_规则与模板/系统状态.md
  90_待确认/00_待确认总表.md
].freeze
REQUIRED_FIELDS = %w[
  memory_id category memory_scope memory_kind origin_type status evidence_level
  sensitivity source_system source_check fact_status source_ref observed_at
  reviewed_at valid_until supersedes content_hash applies_to
].freeze
ALLOWED = {
  "category" => %w[business_cognition internal_material external_communication conversation_summary incoming],
  "memory_scope" => %w[internal_nonpublic mixed conversation_source],
  "memory_kind" => %w[fact judgment decision status open_question guardrail source_index conversation_summary],
  "origin_type" => %w[owner_statement internal_file internal_communication customer_communication local_observation system_observation chatgpt_conversation mixed],
  "status" => %w[active review_required superseded expired quarantined],
  "evidence_level" => %w[user_reported internal_source external_verified unverified inference],
  "sensitivity" => %w[internal confidential public],
  "source_system" => %w[obsidian chatgpt codex workbuddy dingtalk wechat email windows_quick_capture windows_folder_watch],
  "source_check" => %w[unverified matched discrepant],
  "fact_status" => %w[confirmed pending conflicting not_applicable]
}.freeze
REQUIRED_BASELINE_CURATED_IDS = JSON.parse(ENV.fetch("SYNC_CENTER_BASELINE_IDS_JSON", "[]")).tap do |ids|
  raise "Expected an array of explicitly configured baseline IDs" unless ids.is_a?(Array) && ids.all? { |id| id.is_a?(String) }
end.freeze
SEMANTIC_STATUSES = %w[pending manual_merged merged no_business_change tombstone_recorded failed blocked].freeze
SEMANTIC_PROCESSED = %w[manual_merged merged no_business_change tombstone_recorded].freeze
MEMORY_ID_RE = /\ABUSINESS-(?:\d{8}-\d{3}|WIN-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\z/
WIN_ID_RE = /\ABUSINESS-WIN-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\z/
UUID_RE = /\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z/
SHA_RE = /\A[0-9a-f]{64}\z/
CONTENT_SHA_RE = /\Asha256:([0-9a-f]{64})\z/
BUSINESS_ROUTER_RELATIVE = "bin/business_page_router.py"
BUSINESS_CLAIM_BASE_RE = /<!-- business-claim-base:v1 ([A-Za-z0-9_-]+) -->/
BUSINESS_CLAIM_BLOCK_RE = /<!-- business-claim:v[12] [A-Za-z0-9_-]+ -->.*?<!-- \/business-claim:[A-Za-z0-9-]+ -->/m
BUSINESS_EXTERNAL_ASSETS_START = "<!-- CLIENT_ALPHA-EXTERNAL-ASSETS:START -->"
BUSINESS_EXTERNAL_ASSETS_END = "<!-- CLIENT_ALPHA-EXTERNAL-ASSETS:END -->"
SERIES_ID_RE = /\A[a-z0-9]+(?:-[a-z0-9]+)*\z/
WINDOWS_QUICK_CAPTURE = "windows_quick_capture"
WINDOWS_FOLDER_WATCH = "windows_folder_watch"
WATCH_NOTES_RELATIVE = "win11-folder-watch/obsidian-notes-archive"
WATCH_OPERATIONS = %w[baseline added modified deleted renamed].freeze
WATCH_TABLE_COLUMNS = {
  "watch_sources" => %w[source_id source_uuid source_system first_seen_at last_event_at last_event_id active_entries],
  "watch_entries" => %w[source_id source_entry_id current_relative_path path_key current_sha256 source_mtime_ns source_modified_at size_bytes version_number storage_class version_ref mirror_relative active deleted_at last_event_id updated_at],
  "watch_versions" => %w[source_id source_entry_id version_number event_id sha256 size_bytes source_relative_path source_modified_at storage_class destination_ref stored_at],
  "source_events" => %w[event_id bundle_sha256 source_system source_id source_entry_id operation source_relative_path previous_source_relative_path previous_sha256 current_sha256 source_modified_at items_json processed_at status],
  "maintenance_state" => %w[key value updated_at]
}.freeze

FORBIDDEN_DIR_NAMES = %w[
  .git .svn .hg node_modules vendor .venv venv env __pycache__
  .pytest_cache .mypy_cache .ruff_cache .tox .nox .gradle .idea
  .playwright-cli build dist out target coverage cmake-build-debug
  cmake-build-release DerivedData
].freeze
FORBIDDEN_CODE_EXTENSIONS = %w[
  .bat .bash .c .cc .cfg .cjs .cmd .command .conf .cpp .cs .css .fish
  .go .gradle .h .hh .hpp .hta .ini .ipynb .java .js .jse .json .jsonl
  .jsx .kt .kts .less .lock .lua .map .mjs .php .pl .properties .ps1
  .psm1 .py .pyw .r .rb .reg .rs .sass .scss .sh .sln .sql .swift
  .toml .ts .tsx .url .vbe .vbs .vue .was .wsf .wsh .xml .yaml .yml .zsh
].freeze
FORBIDDEN_ARCHIVE_EXTENSIONS = %w[.7z .bz2 .gz .rar .tar .tbz .tbz2 .tgz .txz .xz .zip].freeze
FORBIDDEN_DATABASE_EXTENSIONS = %w[.accdb .db .db3 .duckdb .mdb .sqlite .sqlite3].freeze
FORBIDDEN_MACRO_OFFICE_EXTENSIONS = %w[.docm .dotm .xlam .xlsb .xlsm .xltm .potm .ppam .pptm .sldm].freeze
FORBIDDEN_OLD_OFFICE_EXTENSIONS = %w[.doc .dot .pps .ppt .pot .xls .xlt].freeze
FORBIDDEN_BINARY_EXTENSIONS = %w[
  .a .app .class .com .dll .dmg .dylib .exe .jar .lib .lnk .msi .msp
  .o .pkg .scr .so .war .wasm .whl
].freeze
OOXML_ROOTS = { ".docx" => "word/", ".xlsx" => "xl/", ".pptx" => "ppt/" }.freeze
SAFE_ZIP_DOCUMENT_EXTENSIONS = %w[.emmx].freeze
FORBIDDEN_EMMX_MEMBER_EXTENSIONS = %w[
  .app .bat .class .cmd .com .dll .dylib .exe .hta .jar .js .jse .msi
  .php .pl .ps1 .py .rb .scr .sh .so .swift .vbe .vbs .wasm .wsf .wsh
].freeze
BUILD_DIR_PATTERNS = [/\A\.codex-/, /\Acmake-build-/].freeze
HTML_DYNAMIC_MARKERS = {
  "script element" => /<\s*script\b/i,
  "inline event handler" => /\bon[a-z][a-z0-9_-]*\s*=/i,
  "javascript URL" => /javascript\s*:/i,
  "active embedded element" => /<\s*(?:iframe|object|embed|applet|form)\b/i,
  "meta refresh" => /<\s*meta\b[^>]*http-equiv\s*=\s*["']?\s*refresh\b/i,
  "dynamic evaluation" => /\b(?:eval|Function)\s*\(/i,
  "network API" => /\b(?:fetch|XMLHttpRequest|WebSocket)\s*(?:\(|\b)/i,
  "document write" => /\bdocument\s*\.\s*write(?:ln)?\s*\(/i,
  "service worker" => /\bserviceWorker\b/i,
  "location mutation" => /\b(?:window\s*\.\s*)?location\s*(?:=|\.\s*(?:assign|replace)\s*\()/i
}.freeze

def add_error(errors, message)
  errors << message.to_s
end

def validate_created_heading_suffix!(suffix)
  cleaned = suffix.gsub(BUSINESS_CLAIM_BLOCK_RE, "")
  heading = /\A\r?\n## 核验说明[ \t]*(?:\r?\n|\z)/.match(cleaned)
  raise "claim-base untracked suffix" unless heading

  remainder = cleaned[heading.end(0)..] || ""
  return if remainder.match?(/\A[ \t\r\n]*\z/)

  unless remainder.scan(BUSINESS_EXTERNAL_ASSETS_START).length == 1 &&
         remainder.scan(BUSINESS_EXTERNAL_ASSETS_END).length == 1
    raise "claim-base untracked suffix"
  end
  start_at = remainder.index(BUSINESS_EXTERNAL_ASSETS_START)
  end_at = remainder.index(
    BUSINESS_EXTERNAL_ASSETS_END,
    start_at + BUSINESS_EXTERNAL_ASSETS_START.length
  )
  raise "claim-base untracked suffix" unless end_at

  before = remainder[0...start_at]
  after = remainder[(end_at + BUSINESS_EXTERNAL_ASSETS_END.length)..] || ""
  unless before.match?(/\A[ \t\r\n]*\z/) && after.match?(/\A[ \t\r\n]*\z/)
    raise "claim-base untracked suffix"
  end
end

def annotation_managed_source_hashes(path)
  return [] unless File.extname(path.to_s).downcase == ".md"

  text = File.read(path, encoding: "UTF-8")
  matches = text.to_enum(:scan, BUSINESS_CLAIM_BASE_RE).map { Regexp.last_match.dup }
  return [] if matches.empty?
  raise "multiple claim-base markers" unless matches.length == 1

  match = matches.first
  encoded = match[1]
  encoded += "=" * ((4 - encoded.length % 4) % 4)
  payload = JSON.parse(Base64.urlsafe_decode64(encoded))
  raise "invalid claim-base schema" unless payload.is_a?(Hash) && payload["schema_version"] == 1
  base_sha = payload["base_sha256"].to_s
  trailing = payload["base_trailing"]
  history = payload["annotation_hashes"]
  raise "invalid claim-base hash" unless base_sha.match?(SHA_RE)
  raise "invalid claim-base trailing state" unless trailing.is_a?(String) && trailing.bytesize <= 4096
  unless payload["created_heading"] == true || payload["created_heading"] == false
    raise "invalid claim-base heading state"
  end
  unless history.is_a?(Array) && history.all? { |value| value.to_s.match?(SHA_RE) }
    raise "invalid claim-base annotation history"
  end

  base = if payload["created_heading"]
           validate_created_heading_suffix!(text[match.end(0)..] || "")
           text[0...match.begin(0)].sub(/\s*\z/, "") + trailing
         else
           cleaned = text.gsub(BUSINESS_CLAIM_BLOCK_RE, "")
           cleaned = cleaned.sub(/<!-- business-claim-base:v1 [A-Za-z0-9_-]+ -->[ \t]*(?:\r?\n)?/, "")
           cleaned.sub(/\s*\z/, "") + trailing
         end
  raise "claim-base hash mismatch" unless Digest::SHA256.hexdigest(base) == base_sha

  ([base_sha] + history.map(&:to_s)).uniq
end

def load_business_claims(bridge_root, config_path, errors)
  router = File.join(bridge_root, BUSINESS_ROUTER_RELATIVE)
  stdout, stderr, status = Open3.capture3(
    "/usr/bin/python3", router, "--config", config_path, "list"
  )
  unless status.success?
    add_error(errors, "business claim scan failed: #{stderr.to_s.strip}")
    return []
  end
  payload = JSON.parse(stdout)
  claims = payload["claims"]
  unless claims.is_a?(Array)
    add_error(errors, "business claim scan returned an invalid payload")
    return []
  end
  claims
rescue JSON::ParserError, Errno::ENOENT => e
  add_error(errors, "business claim scan failed: #{e.class}: #{e.message}")
  []
end

def validate_business_claims(bridge_root, config_path, errors)
  router = File.join(bridge_root, BUSINESS_ROUTER_RELATIVE)
  stdout, stderr, status = Open3.capture3(
    "/usr/bin/python3", router, "--config", config_path, "validate"
  )
  payload = JSON.parse(stdout)
  Array(payload["errors"]).each { |issue| add_error(errors, issue) }
  add_error(errors, "business claim validator failed: #{stderr.to_s.strip}") unless status.success? || payload["verdict"] == "fail"
rescue JSON::ParserError, Errno::ENOENT => e
  add_error(errors, "business claim validator failed: #{e.class}: #{e.message}")
end

def ignored_system_or_office_lock?(path)
  basename = File.basename(path)
  basename == ".DS_Store" || basename.start_with?("~$") || basename.start_with?(".~")
end

# Claude Code saves "always allow" answers to <folder>/.claude/example-host.invalid.json
# for any session started in this folder. That file stopped every sync on
# 2020-01-02 and again on 2020-01-02. A pure allow/deny/ask list is not
# executable content, so exactly that is tolerated; hooks, env, apiKeyHelper,
# defaultMode or any other file under .claude/ stay forbidden.
WIN11_SHARE_ROOT = ENV.fetch("SYNC_CENTER_WINDOWS_SHARE", File.join(ENV.fetch("SYNC_CENTER_HOME"), "windows-share"))

# The share is a separate SMB mount; when it drops, the directory is left on the
# local disk (same device as its parent) or disappears.
def win11_share_mounted?
  File.directory?(WIN11_SHARE_ROOT) && File.stat(WIN11_SHARE_ROOT).dev != File.stat(File.dirname(WIN11_SHARE_ROOT)).dev
rescue StandardError
  false
end

CLAUDE_PERMISSION_LIST_KEYS = %w[allow deny ask].freeze

def claude_permission_list?(path, vault_root)
  return false unless File.expand_path(path) == File.expand_path(File.join(vault_root, ".claude", "example-host.invalid.json"))
  return false if File.size(path) > 64 * 1024

  data = JSON.parse(File.read(path, encoding: "UTF-8"))
  return false unless data.is_a?(Hash) && data.keys == ["permissions"] && data["permissions"].is_a?(Hash)

  data["permissions"].all? do |key, value|
    CLAUDE_PERMISSION_LIST_KEYS.include?(key) && value.is_a?(Array) && value.all? { |rule| rule.is_a?(String) }
  end
rescue StandardError
  false
end

def atomic_write_json(path, payload)
  FileUtils.mkdir_p(File.dirname(path))
  temporary = "#{path}.tmp-#{Process.pid}"
  File.open(temporary, "wb", 0o600) do |file|
    file.write(JSON.pretty_generate(payload) + "\n")
    file.flush
    file.fsync
  end
  File.chmod(0o600, temporary)
  File.rename(temporary, path)
ensure
  File.unlink(temporary) if defined?(temporary) && temporary && File.exist?(temporary)
end

def read_utf8(path, errors)
  $validation_progress&.count(:text_reads)
  bytes = File.binread(path)
  text = bytes.force_encoding(Encoding::UTF_8)
  unless text.valid_encoding?
    add_error(errors, "#{path}: invalid UTF-8")
    text = text.scrub
  end
  text.sub(/\A\uFEFF/, "").gsub("\r\n", "\n").gsub("\r", "\n")
rescue StandardError => e
  add_error(errors, "#{path}: unreadable #{e.class}: #{e.message}")
  nil
end

def parse_frontmatter(text)
  match = text.match(/\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\z)(.*)\z/m)
  return nil unless match

  data = YAML.safe_load(
    match[1],
    permitted_classes: [Date, Time],
    permitted_symbols: [],
    aliases: false
  ) || {}
  raise TypeError, "frontmatter must be a mapping" unless data.is_a?(Hash)

  [data, match[2], match[1]]
end

OVERVIEW_TIME_FIELDS = %w[
  generated_at content_reviewed_at last_event_ingested_at last_successful_full_scan_at
].freeze

def overview_time_metadata_issues(data)
  return ["front matter is not an object"] unless data.is_a?(Hash)

  issues = OVERVIEW_TIME_FIELDS.reject { |field| data.key?(field) }.map do |field|
    "missing #{field}"
  end
  issues << "legacy updated_at is forbidden" if data.key?("updated_at")
  OVERVIEW_TIME_FIELDS.each do |field|
    value = data[field]
    if field == "generated_at" && value.nil?
      issues << "generated_at cannot be null"
      next
    end
    next if value.nil? || value.is_a?(Time)
    begin
      Time.iso8601(value.to_s)
    rescue ArgumentError
      issues << "invalid #{field}"
    end
  end
  issues
end

STATUS_COUNT_FIELDS = %w[
  memory_notes formal_memory_notes formal_current_notes conversation_pending_notes
  quick_capture_notes inline_pending_claims external_watch_notes external_watch_pending_notes
].freeze

def normalized_time_metadata(value)
  return nil if value.nil?
  return value.iso8601 if value.respond_to?(:iso8601)

  Time.iso8601(value.to_s).iso8601
rescue ArgumentError
  value.to_s
end

def status_snapshot_issues(status, overview, expected)
  return ["status front matter is not an object"] unless status.is_a?(Hash)
  return ["overview front matter is not an object"] unless overview.is_a?(Hash)

  required = %w[
    document_type version generated_at content_reviewed_at snapshot_source
    last_event_ingested_at last_successful_full_scan_at index_dirty_generation
    index_applied_generation index_last_refreshed_at index_last_error index_state applies_to
  ] + STATUS_COUNT_FIELDS
  issues = required.reject { |field| status.key?(field) }.map { |field| "missing #{field}" }
  issues << "document_type must be memory_system_status" unless status["document_type"] == "memory_system_status"
  issues << "version must be 7.0" unless status["version"].to_f == 7.0
  issues << "snapshot_source must be refresh_memory_indexes" unless status["snapshot_source"] == "refresh_memory_indexes"
  issues << "content_reviewed_at must be null for generated status" unless status["content_reviewed_at"].nil?
  issues << "applies_to must equal project:business-energy" unless status["applies_to"] == ["project:business-energy"]
  if normalized_time_metadata(status["generated_at"]) != normalized_time_metadata(overview["generated_at"])
    issues << "generated_at does not match overview"
  end
  %w[last_event_ingested_at last_successful_full_scan_at].each do |field|
    if normalized_time_metadata(status[field]) != normalized_time_metadata(overview[field])
      issues << "#{field} does not match overview"
    end
  end
  STATUS_COUNT_FIELDS.each do |field|
    issues << "#{field} does not match current state" unless status[field] == expected[field]
  end
  %w[index_dirty_generation index_applied_generation].each do |field|
    issues << "#{field} does not match bridge database" unless status[field] == expected[field]
  end
  if normalized_time_metadata(status["index_last_refreshed_at"]) != normalized_time_metadata(expected["index_last_refreshed_at"])
    issues << "index_last_refreshed_at does not match bridge database"
  end
  issues << "index_last_error does not match bridge database" unless status["index_last_error"].to_s == expected["index_last_error"].to_s
  issues << "index_state does not match bridge database" unless status["index_state"] == expected["index_state"]
  issues << "index state is not healthy" unless status["index_state"] == "up_to_date" && status["index_last_error"].to_s.empty?
  issues
end

def canonical_memory_body(body)
  "\n\n#{body.to_s.sub(/\A\n+/, "")}"
end

def safe_relative?(relative)
  value = relative.to_s
  return false if value.empty? || value.include?("\0") || value.include?("\\")
  return false if Pathname.new(value).absolute?

  parts = value.split("/", -1)
  !parts.empty? && parts.none? { |part| part.empty? || part == "." || part == ".." }
rescue StandardError
  false
end

def contained_path(root, relative)
  return nil unless safe_relative?(relative)

  expanded_root = File.expand_path(root)
  candidate = File.expand_path(relative, expanded_root)
  candidate if candidate.start_with?("#{expanded_root}#{File::SEPARATOR}")
end

def real_contained_path(root, relative)
  candidate = contained_path(root, relative)
  return nil unless candidate && File.exist?(candidate)

  real_root = File.realpath(root)
  real_candidate = File.realpath(candidate)
  return nil unless real_candidate == real_root || real_candidate.start_with?("#{real_root}#{File::SEPARATOR}")

  real_candidate
rescue StandardError
  nil
end

def time_value?(value)
  return true if value.nil? || value.is_a?(Time) || value.is_a?(Date) || value.is_a?(DateTime)

  Time.parse(value.to_s)
  true
rescue ArgumentError, TypeError
  false
end

# Seed of vault top-level folders a source_ref path may start from. With a
# vault_root, every current top-level folder is added (source_ref_vault_tops),
# so renamed or new folders are recognised without editing this list. The
# pre-2020-01-02 names stay so references still written with them are found
# and reported as missing rather than silently skipped.
SOURCE_REF_VAULT_TOPS = %w[
  00_业务记忆库 AI\ 赋能 术语 公司资料 客户及展会 市场与竞品研究 外部调研 招聘
  船舶业务网站与品牌架构 周报 报销材料 其他
  竞品研究 市场部 周报月报绩效
].freeze
SOURCE_REF_PATH_RE_CACHE = {}

def source_ref_vault_tops(vault_root)
  tops = SOURCE_REF_VAULT_TOPS.dup
  root = vault_root.to_s
  if !root.empty? && File.directory?(root)
    Dir.children(root).each do |name|
      next if name.start_with?(".")
      full = File.join(root, name)
      tops << name if File.directory?(full) && !File.symlink?(full)
    end
  end
  # Longest first, so a longer folder name wins over a shorter one it contains.
  tops.uniq.sort_by { |top| -top.length }
end

def source_ref_vault_path_re(tops)
  top_re = Regexp.union(tops)
  # The lookbehind stops a match from starting mid-path: in
  # 市场部/船舶业务网站与品牌架构/x.md the inner folder must not be taken as a
  # separate top-level path (it was, and reported a path that never existed).
  %r{
    (?<![/\p{L}\p{N}_.\-])
    (?:#{top_re})/
    [^;；\n]*?
    \.[A-Za-z0-9]{1,8}
    (?=\z|[\s（()）\[\]【】《》「」『』，、,@])
  }x
end

def path_like_source_refs(source_ref, vault_root = nil)
  value = source_ref.to_s.strip
  return [] if value.empty?

  path_re = (SOURCE_REF_PATH_RE_CACHE[vault_root.to_s] ||= source_ref_vault_path_re(source_ref_vault_tops(vault_root)))

  refs = []
  value.split(/[;；\n]+/).each do |raw_fragment|
    fragment = raw_fragment.to_s.strip
    fragment = fragment[1..-2].to_s.strip if fragment.start_with?("`") && fragment.end_with?("`")
    next if fragment.empty?

    if Pathname.new(fragment).absolute?
      refs << fragment
      next
    end
    fragment.scan(path_re) { |match| refs << match }
  end
  refs.uniq
rescue StandardError
  []
end

def resolve_source_ref_path(vault_root, source_path_ref)
  if Pathname.new(source_path_ref).absolute?
    return nil unless File.exist?(source_path_ref) && !File.symlink?(source_path_ref)

    File.realpath(source_path_ref)
  else
    real_contained_path(vault_root, source_path_ref)
  end
rescue StandardError
  nil
end

def review_metadata_issues(data)
  issues = []
  reviewed_at = data["reviewed_at"]
  reviewed_by = data["reviewed_by"]
  review_mode = data["review_mode"]
  if reviewed_at
    issues << "reviewed_at requires reviewed_by and review_mode" if reviewed_by.to_s.strip.empty? || review_mode.to_s.strip.empty?
  elsif !reviewed_by.to_s.strip.empty? || !review_mode.to_s.strip.empty?
    issues << "reviewed_by/review_mode require reviewed_at"
  end
  issues << "review_required must not claim reviewed_at" if data["status"] == "review_required" && reviewed_at
  issues
end

def supersession_cycles(graph)
  colors = {}
  stack = []
  cycles = []
  visit = lambda do |memory_id|
    if colors[memory_id] == :visiting
      start = stack.index(memory_id) || 0
      cycles << (stack[start..] + [memory_id])
      return
    end
    return if colors[memory_id] == :done

    colors[memory_id] = :visiting
    stack << memory_id
    Array(graph[memory_id]).each { |target| visit.call(target) if graph.key?(target) }
    stack.pop
    colors[memory_id] = :done
  end
  graph.each_key { |memory_id| visit.call(memory_id) }
  cycles.uniq
end

def series_contract_issues(records)
  issues = []
  records.group_by { |record| record.fetch("data")["series_id"] }.each do |series_id, series_records|
    next if series_id.nil? || series_id.to_s.empty?

    current = series_records.select do |record|
      data = record.fetch("data")
      data["status"] == "active" && data["fact_status"] == "confirmed"
    end
    issues << "series #{series_id}: multiple active confirmed records" if current.length > 1
    by_id = records.to_h { |record| [record.fetch("data")["memory_id"].to_s, record] }
    series_records.each do |record|
      Array(record.fetch("data")["supersedes"]).each do |target_id|
        target = by_id[target_id.to_s]
        next unless target && target.fetch("data")["series_id"] == series_id
        if record.fetch("data")["status"] == "active" && target.fetch("data")["status"] != "superseded"
          issues << "#{record.fetch("data")["memory_id"]}: active series successor requires #{target_id} to be superseded"
        end
      end
    end
  end
  issues
end

def count_values(values)
  values.each_with_object(Hash.new(0)) do |value, counts|
    key = value.to_s.empty? ? "(missing)" : value.to_s
    counts[key] += 1
  end.sort.to_h
end

def forbidden_build_dir?(name)
  FORBIDDEN_DIR_NAMES.include?(name) || BUILD_DIR_PATTERNS.any? { |pattern| name.match?(pattern) }
end

def validate_ooxml(path, extension, errors)
  stdout, stderr, status = Open3.capture3("/usr/bin/unzip", "-Z1", path)
  unless status.success?
    detail = stderr.to_s.strip.gsub(/\s+/, " ")[0, 200]
    add_error(errors, "#{path}: invalid OOXML ZIP directory#{detail.empty? ? "" : " (#{detail})"}")
    return
  end

  entries = stdout.to_s.encode("UTF-8", invalid: :replace, undef: :replace).lines.map(&:strip).reject(&:empty?)
  add_error(errors, "#{path}: empty OOXML ZIP") if entries.empty?
  add_error(errors, "#{path}: OOXML missing [Content_Types].xml") unless entries.include?("[Content_Types].xml")
  expected_root = OOXML_ROOTS.fetch(extension)
  add_error(errors, "#{path}: OOXML package does not contain #{expected_root}") unless entries.any? { |entry| entry.start_with?(expected_root) }
  entries.each do |entry|
    if entry.start_with?("/") || entry.include?("\\") || entry.split("/").any? { |part| part == ".." }
      add_error(errors, "#{path}: unsafe OOXML member #{entry}")
    end
  end
  if entries.any? { |entry| entry.match?(%r{(?:\A|/)(?:vbaProject\.bin|activeX(?:/|\z)|customUI(?:/|\z))}i) }
    add_error(errors, "#{path}: macro, ActiveX, or customUI content is forbidden")
  end

  _test_out, test_err, test_status = Open3.capture3("/usr/bin/unzip", "-tqq", path)
  unless test_status.success?
    detail = test_err.to_s.strip.gsub(/\s+/, " ")[0, 200]
    add_error(errors, "#{path}: OOXML ZIP integrity check failed#{detail.empty? ? "" : " (#{detail})"}")
  end
rescue StandardError => e
  add_error(errors, "#{path}: OOXML validation error #{e.class}: #{e.message}")
end

def validate_emmx(path, errors)
  stdout, stderr, status = Open3.capture3("/usr/bin/unzip", "-Z1", path)
  unless status.success?
    detail = stderr.to_s.strip.gsub(/\s+/, " ")[0, 200]
    add_error(errors, "#{path}: invalid EMMX ZIP directory#{detail.empty? ? "" : " (#{detail})"}")
    return
  end

  entries = stdout.to_s.encode("UTF-8", invalid: :replace, undef: :replace).lines.map(&:strip).reject(&:empty?)
  add_error(errors, "#{path}: empty EMMX ZIP") if entries.empty?
  add_error(errors, "#{path}: EMMX missing document.xml") unless entries.include?("document.xml")
  add_error(errors, "#{path}: EMMX missing page/page.xml") unless entries.include?("page/page.xml")
  entries.each do |entry|
    if entry.start_with?("/") || entry.include?("\\") || entry.split("/").any? { |part| part == ".." }
      add_error(errors, "#{path}: unsafe EMMX member #{entry}")
    end
    extension = File.extname(entry).downcase
    if FORBIDDEN_EMMX_MEMBER_EXTENSIONS.include?(extension)
      add_error(errors, "#{path}: executable or script EMMX member #{entry}")
    end
  end

  _test_out, test_err, test_status = Open3.capture3("/usr/bin/unzip", "-tqq", path)
  unless test_status.success?
    detail = test_err.to_s.strip.gsub(/\s+/, " ")[0, 200]
    add_error(errors, "#{path}: EMMX ZIP integrity check failed#{detail.empty? ? "" : " (#{detail})"}")
  end
rescue StandardError => e
  add_error(errors, "#{path}: EMMX validation error #{e.class}: #{e.message}")
end

def validate_static_html(path, errors)
  html = File.binread(path).force_encoding(Encoding::UTF_8)
  unless html.valid_encoding?
    add_error(errors, "#{path}: HTML is not valid UTF-8")
    html = html.scrub
  end
  HTML_DYNAMIC_MARKERS.each do |label, pattern|
    add_error(errors, "#{path}: dynamic HTML marker detected (#{label})") if html.match?(pattern)
  end
rescue StandardError => e
  add_error(errors, "#{path}: HTML validation error #{e.class}: #{e.message}")
end

def validate_file_magic(path, extension, errors)
  # A bounded read at EOF returns nil on the deployed Ruby runtime.
  prefix = File.binread(path, 16) || "".b
  executable_magics = [
    "MZ".b, "\x7FELF".b, "\xFE\xED\xFA\xCE".b, "\xCE\xFA\xED\xFE".b,
    "\xFE\xED\xFA\xCF".b, "\xCF\xFA\xED\xFE".b, "\xCA\xFE\xBA\xBE".b,
    "\xBE\xBA\xFE\xCA".b
  ]
  if executable_magics.any? { |magic| prefix.start_with?(magic) }
    add_error(errors, "#{path}: executable/program signature is forbidden")
  end
  if prefix.start_with?("\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1".b)
    add_error(errors, "#{path}: legacy Compound File/old Office signature is forbidden")
  end
  add_error(errors, "#{path}: database signature is forbidden") if prefix.start_with?("SQLite format 3\0".b)
  if prefix.start_with?("PK\x03\x04".b, "PK\x05\x06".b, "PK\x07\x08".b) &&
     !OOXML_ROOTS.key?(extension) && !SAFE_ZIP_DOCUMENT_EXTENSIONS.include?(extension)
    add_error(errors, "#{path}: ZIP/archive signature is forbidden outside OOXML")
  end
  if prefix.start_with?("\x1F\x8B".b, "7z\xBC\xAF\x27\x1C".b, "Rar!\x1A\x07".b)
    add_error(errors, "#{path}: archive signature is forbidden")
  end
  add_error(errors, "#{path}: script shebang is forbidden") if prefix.start_with?("#!".b)
rescue StandardError => e
  add_error(errors, "#{path}: signature check error #{e.class}: #{e.message}")
end

def scan_storage_boundary(vault_root, errors)
  counts = Hash.new(0)
  Find.find(vault_root) do |path|
    begin
      stat = File.lstat(path)
    rescue StandardError => e
      add_error(errors, "#{path}: cannot inspect storage entry #{e.class}: #{e.message}")
      next
    end
    if stat.symlink?
      add_error(errors, "#{path}: symlinks are forbidden in the BUSINESS storage boundary")
      next
    end
    if stat.directory?
      name = File.basename(path)
      if path != vault_root && forbidden_build_dir?(name)
        add_error(errors, "#{path}: code/build directory is forbidden")
        Find.prune
      end
      next
    end
    next unless stat.file?
    if ignored_system_or_office_lock?(path)
      counts["ignored_system_or_office_lock"] += 1
      next
    end
    if claude_permission_list?(path, vault_root)
      counts["ignored_claude_permission_list"] += 1
      next
    end

    counts["files_scanned"] += 1
    extension = File.extname(path).downcase
    category = if FORBIDDEN_CODE_EXTENSIONS.include?(extension)
                 "script/code/config"
               elsif FORBIDDEN_ARCHIVE_EXTENSIONS.include?(extension)
                 "archive"
               elsif FORBIDDEN_DATABASE_EXTENSIONS.include?(extension)
                 "database"
               elsif FORBIDDEN_MACRO_OFFICE_EXTENSIONS.include?(extension)
                 "macro-enabled Office"
               elsif FORBIDDEN_OLD_OFFICE_EXTENSIONS.include?(extension)
                 "legacy Office"
               elsif FORBIDDEN_BINARY_EXTENSIONS.include?(extension)
                 "program/binary"
               end
    add_error(errors, "#{path}: forbidden #{category} file type #{extension}") if category
    validate_file_magic(path, extension, errors)
    validate_ooxml(path, extension, errors) if OOXML_ROOTS.key?(extension)
    validate_emmx(path, errors) if extension == ".emmx"
    validate_static_html(path, errors) if %w[.html .htm].include?(extension)
  end
  counts.sort.to_h
rescue StandardError => e
  add_error(errors, "#{vault_root}: storage scan aborted #{e.class}: #{e.message}")
  counts.sort.to_h
end

def resolve_wikilink(raw_link, current_path, vault_root, memory_index, vault_index, basename_index)
  link = raw_link.to_s.strip.sub(/\.md\z/i, "")
  link = link.delete_prefix("#{File.basename(vault_root)}/")
  return [:missing, nil] if link.empty?

  if link.include?("/")
    candidates = []
    candidates << memory_index[link] if memory_index.key?(link)
    candidates << vault_index[link] if vault_index.key?(link)
    begin
      local = Pathname.new(File.dirname(current_path)).join("#{link}.md").cleanpath
      local_relative = local.relative_path_from(Pathname.new(vault_root)).to_s.sub(/\.md\z/, "")
      candidates << vault_index[local_relative] if vault_index.key?(local_relative)
    rescue ArgumentError
      # Outside-vault paths stay unresolved.
    end
    candidates = candidates.compact.uniq
  else
    candidates = Array(basename_index[link]).uniq
  end
  return [:missing, nil] if candidates.empty?
  return [:ambiguous, nil] if candidates.length > 1

  [:ok, candidates.first]
end

def validate_links(path, text, vault_root, memory_index, vault_index, basename_index, errors)
  text.scan(/\[\[([^\]]+)\]\]/).flatten.each do |inner|
    raw_link = inner.split("|", 2).first.to_s.split("#", 2).first.to_s.strip
    state, = resolve_wikilink(raw_link, path, vault_root, memory_index, vault_index, basename_index)
    add_error(errors, "#{path}: unresolved link #{raw_link}") if state == :missing
    add_error(errors, "#{path}: ambiguous link #{raw_link}") if state == :ambiguous
  end
end

def normalize_index_id(value)
  id = value.to_s.strip
  return "BUSINESS-20260824-#{id}" if id.match?(/\A\d{3}\z/)
  return "BUSINESS-#{id}" if id.match?(/\A\d{8}-\d{3}\z/)
  return "BUSINESS-#{id}" if id.match?(/\AWIN-[0-9a-f-]{36}\z/)
  return id if id.start_with?("BUSINESS-")

  nil
end

def parse_index_rows(path, text, errors)
  rows = []
  text.each_line.with_index(1) do |line, line_number|
    next unless line.lstrip.start_with?("|")

    cells = line.split("|", -1).map(&:strip)
    next if cells.length < 5
    display_id = cells[1]
    next if display_id.empty? || %w[ID --- —].include?(display_id)

    memory_id = normalize_index_id(display_id)
    next unless memory_id
    legacy = cells.length >= 8
    link_cell = legacy ? cells[6] : cells[3]
    link_match = link_cell.to_s.match(/\[\[([^\]|#]+)/)
    unless link_match
      add_error(errors, "#{path}:#{line_number}: index row #{display_id} lacks a note link")
      next
    end
    rows << {
      "memory_id" => memory_id, "status" => legacy ? cells[3] : nil,
      "source_check" => legacy ? cells[4] : nil, "fact_status" => legacy ? cells[5] : nil,
      "link" => link_match[1].strip.sub(/\.md\z/i, ""), "line" => line_number
    }
  end
  rows
end

def validate_memory_index(path, text, rows, expected_records, records_by_path, memory_root, errors, allow_omitted: false)
  expected_ids = expected_records.map { |record| record.fetch("data")["memory_id"].to_s }
  grouped = rows.group_by { |row| row["memory_id"] }
  missing = expected_ids - grouped.keys
  extra = grouped.keys - expected_ids
  if allow_omitted
    frontmatter = parse_frontmatter(text)
    declared = frontmatter && frontmatter[0]["omitted_records"]
    add_error(errors, "#{path}: omitted_records must be a non-negative integer") unless declared.is_a?(Integer) && declared >= 0
    if declared.is_a?(Integer) && declared != missing.length
      add_error(errors, "#{path}: omitted_records #{declared} does not match #{missing.length} omitted IDs")
    end
    if frontmatter && frontmatter[0]["displayed_records"] != grouped.length
      add_error(errors, "#{path}: displayed_records does not match index rows")
    end
  else
    add_error(errors, "#{path}: missing index IDs #{missing.join(",")}") unless missing.empty?
  end
  add_error(errors, "#{path}: unexpected index IDs #{extra.join(",")}") unless extra.empty?
  grouped.each do |memory_id, copies|
    add_error(errors, "#{path}: duplicate index row #{memory_id}") unless copies.length == 1
    copies.each do |row|
      record = expected_records.find { |candidate| candidate.fetch("data")["memory_id"].to_s == memory_id }
      next unless record

      data = record.fetch("data")
      %w[status source_check fact_status].each do |field|
        if !row[field].nil? && row[field] != data[field].to_s
          add_error(errors, "#{path}:#{row["line"]}: #{memory_id} #{field} #{row[field].inspect} != #{data[field].inspect}")
        end
      end
      linked_path = File.join(memory_root, "#{row["link"]}.md")
      linked_relative = if File.file?(linked_path)
                          Pathname.new(linked_path).relative_path_from(Pathname.new(memory_root)).to_s
                        end
      target = linked_relative && records_by_path[linked_relative]
      if target.nil?
        add_error(errors, "#{path}:#{row["line"]}: #{memory_id} points outside the memory-note set (#{row["link"]})")
      elsif target.fetch("data")["memory_id"].to_s != memory_id
        add_error(errors, "#{path}:#{row["line"]}: #{memory_id} link points to #{target.fetch("data")["memory_id"]}")
      end
    end
  end
end

def validate_external_watch_notes(root, normal_records_by_id, errors)
  records = []
  records_by_id = {}
  unless File.directory?(root) && !File.symlink?(root)
    add_error(errors, "#{root}: external watch-note directory is missing or unsafe")
    return [records, records_by_id]
  end
  root_mode = File.stat(root).mode & 0o777
  add_error(errors, "#{root}: external watch-note directory must be owner-only") unless (root_mode & 0o077).zero?
  real_root = File.realpath(root)

  Dir.glob(File.join(root, "**", "*.md")).sort.each do |path|
    basename = File.basename(path)
    context = path
    unless File.dirname(path) == root && basename.match?(/\AWin11文件夹监控-([0-9a-f-]{36})\.md\z/)
      add_error(errors, "#{context}: external watch note must use the flat UUID filename layout")
      next
    end
    filename_entry_id = basename[/\AWin11文件夹监控-([0-9a-f-]{36})\.md\z/, 1]
    unless File.file?(path) && !File.symlink?(path)
      add_error(errors, "#{context}: external watch note must be a regular non-symlink file")
      next
    end
    unless File.realpath(path).start_with?("#{real_root}#{File::SEPARATOR}")
      add_error(errors, "#{context}: external watch note escapes its controlled root")
      next
    end
    mode = File.stat(path).mode & 0o777
    add_error(errors, "#{context}: external watch note must be owner-only") unless (mode & 0o077).zero?
    text = read_utf8(path, errors)
    next unless text
    begin
      parsed = parse_frontmatter(text)
    rescue StandardError => e
      add_error(errors, "#{context}: YAML #{e.class}: #{e.message}")
      next
    end
    if parsed.nil?
      add_error(errors, "#{context}: external watch note lacks frontmatter")
      next
    end
    data, body, raw_frontmatter = parsed
    duplicate_keys = raw_frontmatter.lines.map { |line| line[/\A([A-Za-z_][A-Za-z0-9_-]*):/, 1] }.compact
      .group_by(&:itself).select { |_key, copies| copies.length > 1 }.keys
    add_error(errors, "#{context}: duplicate top-level YAML keys #{duplicate_keys.join(',')}") unless duplicate_keys.empty?
    missing = REQUIRED_FIELDS.reject { |field| data.key?(field) }
    add_error(errors, "#{context}: missing #{missing.join(',')}") unless missing.empty?

    entry_id = data["source_entry_id"].to_s
    memory_id = data["memory_id"].to_s
    expected_memory_id = "BUSINESS-WIN-#{entry_id}"
    add_error(errors, "#{context}: filename UUID mismatch") unless filename_entry_id == entry_id && entry_id.match?(UUID_RE)
    add_error(errors, "#{context}: memory_id must match source_entry_id") unless memory_id == expected_memory_id && memory_id.match?(WIN_ID_RE)
    add_error(errors, "#{context}: duplicate external memory_id #{memory_id}") if records_by_id.key?(memory_id)
    add_error(errors, "#{context}: memory_id conflicts with an Obsidian note") if normal_records_by_id.key?(memory_id)

    expected_fields = {
      "category" => "incoming", "memory_scope" => "internal_nonpublic", "memory_kind" => "status",
      "origin_type" => "system_observation", "status" => "review_required",
      "evidence_level" => "unverified", "sensitivity" => "internal",
      "source_system" => WINDOWS_FOLDER_WATCH, "source_check" => "unverified", "fact_status" => "pending"
    }
    expected_fields.each do |field, expected|
      add_error(errors, "#{context}: #{field} must be #{expected}") unless data[field] == expected
    end
    add_error(errors, "#{context}: applies_to must equal project:business-energy") unless data["applies_to"] == ["project:business-energy"]
    add_error(errors, "#{context}: supersedes must be an array") unless data["supersedes"].is_a?(Array)
    expected_ref = "#{WINDOWS_FOLDER_WATCH}:#{data['source_ref'].to_s.split(':')[1]}:#{entry_id}"
    unless data["source_ref"].to_s.match?(/\Awindows_folder_watch:[A-Za-z0-9._-]{1,128}:[0-9a-f-]{36}\z/) &&
           data["source_ref"] == expected_ref
      add_error(errors, "#{context}: invalid folder-watch source_ref")
    end
    add_error(errors, "#{context}: source_event_id must be a UUID") unless data["source_event_id"].to_s.match?(UUID_RE)
    add_error(errors, "#{context}: invalid source_operation") unless WATCH_OPERATIONS.include?(data["source_operation"].to_s)
    add_error(errors, "#{context}: invalid source_bundle_sha256") unless data["source_bundle_sha256"].to_s.match?(CONTENT_SHA_RE)
    hash_match = data["content_hash"].to_s.match(CONTENT_SHA_RE)
    add_error(errors, "#{context}: invalid content_hash") unless hash_match
    current_hash = Digest::SHA256.hexdigest(canonical_memory_body(body))
    add_error(errors, "#{context}: content_hash does not match normalized Markdown body") if hash_match && hash_match[1] != current_hash

    if data.key?("semantic_status")
      semantic_status = data["semantic_status"].to_s
      add_error(errors, "#{context}: invalid semantic_status #{semantic_status.inspect}") unless SEMANTIC_STATUSES.include?(semantic_status)
      if SEMANTIC_PROCESSED.include?(semantic_status)
        unless data["semantic_run_id"].to_s.match?(/\A[A-Za-z0-9._:-]{1,128}\z/)
          add_error(errors, "#{context}: processed semantic source requires semantic_run_id")
        end
        begin
          Time.iso8601(data["semantic_processed_at"].to_s)
        rescue ArgumentError
          add_error(errors, "#{context}: processed semantic source requires ISO semantic_processed_at")
        end
        unless data["semantic_targets"].is_a?(Array) && data["semantic_targets"].all? { |target| target.to_s.match?(MEMORY_ID_RE) }
          add_error(errors, "#{context}: processed semantic source requires semantic_targets array")
        end
      end
    end
    add_error(errors, "#{context}: duplicate H1") if text.lines.any? { |line| line.strip == "# #{File.basename(path, '.md')}" }
    add_error(errors, "#{context}: possible API key") if text.match?(/\bsk-[A-Za-z0-9_-]{20,}\b|\bAKIA[0-9A-Z]{16}\b/)

    record = { "path" => path, "relative" => basename, "data" => data, "body" => body, "text" => text }
    records << record
    records_by_id[memory_id] = record unless memory_id.empty?
  rescue StandardError => e
    add_error(errors, "#{context}: external watch-note validation error #{e.class}: #{e.message}")
  end
  [records, records_by_id]
end

def load_json_fail_closed(path, errors)
  unless File.file?(path)
    add_error(errors, "#{path}: required JSON is missing")
    return nil
  end
  if File.symlink?(path)
    add_error(errors, "#{path}: JSON must not be a symlink")
    return nil
  end
  parsed = JSON.parse(File.read(path, encoding: "UTF-8"))
  unless parsed.is_a?(Hash)
    add_error(errors, "#{path}: JSON root must be an object")
    return nil
  end
  parsed
rescue StandardError => e
  add_error(errors, "#{path}: invalid JSON #{e.class}: #{e.message}")
  nil
end

def plist_minutes(path, errors)
  stdout, stderr, status = Open3.capture3("/usr/bin/plutil", "-convert", "json", "-o", "-", path)
  unless status.success?
    add_error(errors, "#{path}: cannot read plist schedule #{stderr.to_s.strip}")
    return []
  end
  data = JSON.parse(stdout)
  intervals = data["StartCalendarInterval"]
  intervals = [intervals] if intervals.is_a?(Hash)
  [data["Label"].to_s, Array(intervals).map { |entry| entry["Minute"] if entry.is_a?(Hash) }.compact.map(&:to_i).sort]
rescue StandardError => e
  add_error(errors, "#{path}: invalid plist #{e.class}: #{e.message}")
  []
end

def validate_bridge_config(path, config, memory_root, vault_root, external_argument, errors)
  return {} unless config

  required = %w[
    memory_root external_root watch_notes_root machine_ledger_root semantic_database
    semantic_runs_root memory_writers schedules index_budgets source_governance
  ]
  missing = required.reject { |key| config.key?(key) }
  add_error(errors, "#{path}: missing config keys #{missing.join(",")}") unless missing.empty?
  configured_memory = File.expand_path(config["memory_root"].to_s)
  configured_external = File.expand_path(config["external_root"].to_s)
  configured_ledger = File.expand_path(config["machine_ledger_root"].to_s)
  add_error(errors, "#{path}: memory_root mismatch") if memory_root && configured_memory != memory_root
  if external_argument && File.expand_path(external_argument) != configured_external &&
     File.expand_path(external_argument) != configured_ledger
    add_error(errors, "#{path}: external root argument does not match configured roots")
  end
  if vault_root && (configured_external == vault_root || configured_external.start_with?("#{vault_root}#{File::SEPARATOR}"))
    add_error(errors, "#{path}: external_root must remain outside the BUSINESS vault")
  end
  %w[external_root watch_notes_root machine_ledger_root semantic_runs_root].each do |key|
    value = config[key].to_s
    unless Pathname.new(value).absolute? && File.directory?(value) && !File.symlink?(value)
      add_error(errors, "#{path}: #{key} is missing, relative, or a symlink")
    end
  end
  %w[watch_notes_root machine_ledger_root semantic_runs_root].each do |key|
    value = config[key].to_s
    next unless File.exist?(value) && File.exist?(configured_external)
    real_external = File.realpath(configured_external)
    real_value = File.realpath(value)
    unless real_value.start_with?("#{real_external}#{File::SEPARATOR}")
      add_error(errors, "#{path}: #{key} escapes external_root")
    end
  end
  semantic_database = config["semantic_database"].to_s
  unless Pathname.new(semantic_database).absolute? &&
         File.expand_path(semantic_database).start_with?("#{configured_ledger}#{File::SEPARATOR}")
    add_error(errors, "#{path}: semantic_database must be inside machine_ledger_root")
  end
  governance = config["source_governance"]
  if governance.is_a?(Hash)
    %w[inventory derivation_receipts legacy_ledger].each do |key|
      value = governance[key].to_s
      unless Pathname.new(value).absolute? && File.expand_path(value).start_with?("#{configured_ledger}#{File::SEPARATOR}")
        add_error(errors, "#{path}: source_governance.#{key} must be inside machine_ledger_root")
      end
    end
  else
    add_error(errors, "#{path}: source_governance must be an object")
  end

  serialized = JSON.generate(config)
  add_error(errors, "#{path}: legacy 91_传入资料 path is forbidden") if serialized.include?("91_传入资料")
  add_error(errors, "#{path}: legacy 22:30 schedule is forbidden") if serialized.include?("22:30")
  writers = config["memory_writers"]
  unless writers.is_a?(Array) && !writers.empty?
    add_error(errors, "#{path}: memory_writers must be a non-empty array")
    return config
  end
  labels = {}
  plists = {}
  writers.each_with_index do |writer, index|
    context = "#{path}: memory_writers[#{index}]"
    unless writer.is_a?(Hash)
      add_error(errors, "#{context}: writer must be an object")
      next
    end
    label = writer["label"].to_s
    plist = writer["plist_path"].to_s
    add_error(errors, "#{context}: duplicate/empty label #{label.inspect}") if label.empty? || labels[label]
    add_error(errors, "#{context}: duplicate/empty plist #{plist.inspect}") if plist.empty? || plists[plist]
    labels[label] = true
    plists[plist] = true
    unless %w[upstream_producer scheduled_writer source_listener dormant_writer source_producer sole_receiver].include?(writer["role"])
      add_error(errors, "#{context}: invalid role #{writer["role"].inspect}")
    end
    process_markers = writer["process_markers"]
    unless process_markers.is_a?(Array) && !process_markers.empty? &&
           process_markers.all? { |marker| marker.is_a?(String) && !marker.empty? }
      add_error(errors, "#{context}: process_markers must be a non-empty string array")
    end
    %w[stop_order resume_order].each do |field|
      add_error(errors, "#{context}: #{field} must be a non-negative integer") unless writer[field].is_a?(Integer) && writer[field] >= 0
    end
    unless Pathname.new(plist).absolute? && File.file?(plist) && !File.symlink?(plist)
      add_error(errors, "#{context}: plist is missing, relative, or a symlink")
      next
    end
    actual_label, = plist_minutes(plist, errors)
    add_error(errors, "#{context}: plist Label #{actual_label.inspect} != #{label.inspect}") unless actual_label == label
  end
  expected_schedules = {
    "com.example.business-win11-memory-hourly" => Array(config.dig("schedules", "win11_semantic_minutes")).map(&:to_i).sort,
    "com.example.dingtalk-minutes-memory-source" => Array(config.dig("schedules", "dingtalk_minutes_source_minutes")).map(&:to_i).sort,
    "com.example.dingtalk-minutes-memory-semantic" => Array(config.dig("schedules", "dingtalk_minutes_semantic_minutes")).map(&:to_i).sort
  }
  writers.each do |writer|
    expected = expected_schedules[writer["label"]]
    next unless expected
    _label, actual = plist_minutes(writer["plist_path"], errors)
    add_error(errors, "#{writer["plist_path"]}: schedule #{actual.inspect} != config #{expected.inspect}") unless actual == expected
  end
  budgets = config["index_budgets"]
  if budgets.is_a?(Hash)
    %w[overview ai pending].each do |name|
      soft = budgets["#{name}_soft_tokens"].to_i
      hard = budgets["#{name}_hard_tokens"].to_i
      add_error(errors, "#{path}: invalid #{name} index budgets") unless soft.positive? && hard.positive? && soft <= hard
    end
  else
    add_error(errors, "#{path}: index_budgets must be an object")
  end
  config
end

def validate_conversation_ledger(path, ledger, conversation_records, memory_root, errors)
  return 0 unless ledger

  records = ledger["records"]
  unless records.is_a?(Hash)
    add_error(errors, "#{path}: records must be an object")
    return 0
  end
  if ledger.key?("record_count") && ledger["record_count"] != records.length
    add_error(errors, "#{path}: record_count mismatch")
  end

  note_paths = []
  records.each do |conversation_id, record|
    unless conversation_id.to_s.match?(/\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z/)
      add_error(errors, "#{path}: invalid conversation ID #{conversation_id.inspect}")
    end
    unless record.is_a?(Hash)
      add_error(errors, "#{path}: conversation #{conversation_id} record must be an object")
      next
    end
    relative = record["note"].to_s
    unless safe_relative?(relative) && relative.start_with?("04_对话来源摘要/")
      add_error(errors, "#{path}: unsafe conversation note path #{relative.inspect}")
      next
    end
    note_paths << relative
    note = contained_path(memory_root, relative)
    add_error(errors, "#{path}: missing conversation note #{relative}") unless note && File.file?(note)
    signature = record["signature_hash"].to_s
    add_error(errors, "#{path}: invalid signature_hash for #{conversation_id}") unless signature.match?(CONTENT_SHA_RE)
    corresponding = conversation_records.find { |candidate| candidate.fetch("relative") == relative }
    if corresponding && !corresponding.fetch("data")["source_ref"].to_s.include?(conversation_id.to_s)
      add_error(errors, "#{path}: #{relative} source_ref does not identify #{conversation_id}")
    end
  end

  expected = conversation_records.map { |record| record.fetch("relative") }.sort
  actual = note_paths.sort
  missing = expected - actual
  extra = actual - expected
  add_error(errors, "#{path}: missing conversation notes #{missing.join(",")}") unless missing.empty?
  add_error(errors, "#{path}: unexpected conversation notes #{extra.join(",")}") unless extra.empty?
  add_error(errors, "#{path}: duplicate conversation note references") unless actual.uniq.length == actual.length
  explicit = ledger["explicit_conversation_ids"]
  unless explicit.is_a?(Array)
    add_error(errors, "#{path}: explicit_conversation_ids must be an array")
  else
    explicit.each do |conversation_id|
      add_error(errors, "#{path}: explicit conversation ID not present #{conversation_id}") unless records.key?(conversation_id)
    end
  end
  records.length
end

def validate_internal_ledger(path, ledger, vault_root, memory_root, errors)
  return [0, {}] unless ledger

  records = ledger["records"]
  unless records.is_a?(Array)
    add_error(errors, "#{path}: records must be an array")
    return [0, {}]
  end
  add_error(errors, "#{path}: record_count mismatch") unless ledger["record_count"] == records.length
  add_error(errors, "#{path}: vault_root mismatch") unless File.expand_path(ledger["vault_root"].to_s) == File.expand_path(vault_root)
  add_error(errors, "#{path}: memory_root mismatch") unless File.expand_path(ledger["memory_root"].to_s) == File.expand_path(memory_root)

  keys = {}
  records.each_with_index do |record, index|
    unless record.is_a?(Hash)
      add_error(errors, "#{path}: record #{index} must be an object")
      next
    end
    root_name = record["root"].to_s
    source_ref = record["path"].to_s
    key = [root_name, source_ref]
    if keys.key?(key)
      add_error(errors, "#{path}: duplicate source #{key.join(":")}")
    else
      keys[key] = true
    end
    unless %w[vault local].include?(root_name)
      add_error(errors, "#{path}: invalid source root #{root_name.inspect}")
      next
    end
    if ignored_system_or_office_lock?(source_ref)
      add_error(errors, "#{path}: ignored system/Office lock file must not be indexed #{source_ref}")
    end

    source_path = if root_name == "vault"
                    unless safe_relative?(source_ref)
                      add_error(errors, "#{path}: unsafe vault source path #{source_ref.inspect}")
                      next
                    end
                    contained_path(vault_root, source_ref)
                  else
                    unless Pathname.new(source_ref).absolute? && !source_ref.include?("\0")
                      add_error(errors, "#{path}: local source path must be absolute #{source_ref.inspect}")
                      next
                    end
                    File.expand_path(source_ref)
                  end
    sha256 = record["sha256"].to_s
    add_error(errors, "#{path}: invalid historical sha256 for #{key.join(":")}") unless sha256.match?(SHA_RE)
    add_error(errors, "#{path}: invalid historical size for #{key.join(":")}") unless record["size"].is_a?(Integer) && record["size"] >= 0
    outcome = record["outcome"].to_s
    add_error(errors, "#{path}: empty outcome for #{key.join(":")}") if outcome.empty?
    add_error(errors, "#{path}: empty result_summary for #{key.join(":")}") if record["result_summary"].to_s.strip.empty?
    if outcome.match?(/\A(?:excluded|skipped|indexed|detailed|governance)/) && record["skip_reason"].to_s.strip.empty?
      add_error(errors, "#{path}: skip_reason missing for #{key.join(":")}")
    end
  rescue StandardError => e
    add_error(errors, "#{path}: source #{key.join(":")} validation error #{e.class}: #{e.message}")
  end

  outcomes = count_values(records.map { |record| record["outcome"] if record.is_a?(Hash) }.compact)
  add_error(errors, "#{path}: outcome_counts mismatch") if ledger["outcome_counts"].is_a?(Hash) && ledger["outcome_counts"] != outcomes
  [records.length, outcomes]
rescue StandardError => e
  add_error(errors, "#{path}: internal ledger validation error #{e.class}: #{e.message}")
  [0, {}]
end

def validate_source_inventory(path, inventory, vault_root, memory_root, errors, coordination_points)
  return [0, {}] unless inventory

  records = inventory["records"]
  unless inventory["schema_version"] == 2 && records.is_a?(Array)
    add_error(errors, "#{path}: source inventory requires schema_version 2 and records array")
    return [0, {}]
  end
  add_error(errors, "#{path}: record_count mismatch") unless inventory["record_count"] == records.length
  add_error(errors, "#{path}: vault_root mismatch") unless File.expand_path(inventory["vault_root"].to_s) == File.expand_path(vault_root)
  add_error(errors, "#{path}: memory_root mismatch") unless File.expand_path(inventory["memory_root"].to_s) == File.expand_path(memory_root)
  seen = {}
  observed_change_counts = Hash.new(0)
  records.each_with_index do |record, index|
    unless record.is_a?(Hash)
      add_error(errors, "#{path}: inventory record #{index} must be an object")
      next
    end
    root_name = record["root"].to_s
    source_ref = record["path"].to_s
    key = [root_name, source_ref]
    add_error(errors, "#{path}: duplicate source #{key.join(":")}") if seen[key]
    seen[key] = true
    source_path = if root_name == "vault"
                    unless safe_relative?(source_ref)
                      add_error(errors, "#{path}: unsafe vault source path #{source_ref.inspect}")
                      next
                    end
                    real_contained_path(vault_root, source_ref)
                  elsif root_name == "local" && Pathname.new(source_ref).absolute?
                    File.file?(source_ref) && !File.symlink?(source_ref) ? File.realpath(source_ref) : nil
                  else
                    add_error(errors, "#{path}: invalid source root/path #{key.join(":")}")
                    nil
                  end
    unless source_path && File.file?(source_path)
      add_error(errors, "#{path}: missing or unsafe current source #{key.join(":")}")
      next
    end
    sha256 = record["sha256"].to_s
    unless sha256.match?(SHA_RE)
      add_error(errors, "#{path}: invalid current sha256 for #{key.join(":")}")
      next
    end
    current_hash = Digest::SHA256.file(source_path).hexdigest
    if current_hash != sha256
      coordination_points << "Source inventory drift: #{key.join(":")} changed after #{inventory["generated_at"]}; refresh the inventory before extraction."
    end
    if record["size"].to_i != File.size(source_path)
      coordination_points << "Source inventory size drift: #{key.join(":")}."
    end
    change_state = record["change_state"].to_s
    add_error(errors, "#{path}: invalid change_state for #{key.join(":")}") unless %w[new changed unchanged hash_unavailable].include?(change_state)
    observed_change_counts[change_state] += 1
  rescue StandardError => e
    add_error(errors, "#{path}: inventory source #{key.join(":")} validation error #{e.class}: #{e.message}")
  end
  expected_counts = observed_change_counts.sort.to_h
  add_error(errors, "#{path}: change_counts mismatch") unless inventory["change_counts"] == expected_counts
  [records.length, expected_counts]
end

def validated_source_relocations(receipts, by_id, vault_root, errors, ledger_path)
  resolved = {}
  # A relocation normally binds the moved file to the derivation's original
  # hash. When that derivation's content change is already acknowledged — by a
  # stale_derivation receipt, or because the original hash is the file's
  # annotation-managed base — the file may have moved after it changed. The
  # relocation still records the file's true current hash and still needs the
  # original path gone, authorization and bound evidence; it only stops an
  # acknowledged change from blocking the move. An unacknowledged change is
  # rejected as before. (2020-01-02: 待修订清单_V3_1.md moved folders after
  # edits that were already marked stale.)
  acknowledged_change = receipts.select { |receipt| receipt["receipt_type"] == "stale_derivation" }
                                .to_h { |receipt| [receipt["stale_receipt_id"].to_s, true] }
  receipts.select { |receipt| receipt["receipt_type"] == "source_relocation" }.each do |receipt|
    context = "#{ledger_path}:#{receipt["line_number"]}"
    error_count = errors.length
    previous = receipt["previous_source_path"].to_s
    current = receipt["source_path"].to_s
    sha = receipt["source_sha256"].to_s
    unless receipt["state"] == "applied" && receipt["change_kind"] == "path_only" && receipt["source_root"] == "vault"
      add_error(errors, "#{context}: invalid source relocation contract")
    end
    old_path = contained_path(vault_root, previous)
    if !old_path || previous == current || File.exist?(old_path) || File.symlink?(old_path)
      add_error(errors, "#{context}: relocation requires a missing safe original path")
    end
    current_path = real_contained_path(vault_root, current)
    current_safe = current_path && File.file?(current_path) &&
                   current_path == File.expand_path(current, File.realpath(vault_root))
    # A moved file that is also a governed business page keeps receiving claim
    # blocks through the writer. Its valid claim-base proves the text outside
    # those blocks is unchanged, the same proof derivation receipts accept for
    # pages that never moved; without it every claim routed to a moved page
    # failed validation and was rolled back (2020-01-02, 待修订清单_V3_1.md).
    governed_page = current_safe && Digest::SHA256.file(current_path).hexdigest != sha && begin
      !annotation_managed_source_hashes(current_path).empty?
    rescue StandardError
      false
    end
    unless current_safe && (Digest::SHA256.file(current_path).hexdigest == sha || governed_page)
      add_error(errors, "#{context}: relocated source content is missing, unsafe or changed")
    end
    add_error(errors, "#{context}: source relocation lacks authorization") if receipt["authorized_by"].to_s.strip.empty?
    evidence_ref = receipt["evidence_ref"].to_s
    evidence_sha = receipt["evidence_sha256"].to_s
    proof_valid = begin
      Pathname.new(evidence_ref).absolute? && File.file?(evidence_ref) &&
        File.realpath(evidence_ref) == File.expand_path(evidence_ref) &&
        evidence_sha.match?(SHA_RE) && Digest::SHA256.file(evidence_ref).hexdigest == evidence_sha &&
        Array(JSON.parse(File.read(evidence_ref, encoding: "UTF-8"))["relocations"]).any? do |entry|
          entry.is_a?(Hash) && entry["source_root"] == "vault" &&
            entry["from_path"] == previous && entry["to_path"] == current && entry["sha256"] == sha
        end
    rescue StandardError
      false
    end
    add_error(errors, "#{context}: relocation evidence does not bind this path and hash") unless proof_valid
    targets = receipt["resolved_receipt_ids"]
    unless targets.is_a?(Array) && !targets.empty? && targets.uniq.length == targets.length
      add_error(errors, "#{context}: relocation requires unique derivation receipt IDs")
      targets = []
    end
    managed_hashes = begin
      current_path && File.file?(current_path) ? annotation_managed_source_hashes(current_path) : []
    rescue StandardError
      []
    end
    targets.each do |target_id|
      target = by_id[target_id.to_s]
      original_sha = target && target["source_sha256"].to_s
      content_accounted = target && (original_sha == sha || acknowledged_change[target_id.to_s] ||
                                     managed_hashes.include?(original_sha))
      unless target && target["receipt_type"] == "derivation" && target["source_root"] == "vault" &&
          target["source_path"] == previous && content_accounted
        add_error(errors, "#{context}: relocation target does not match original source and hash")
      end
      add_error(errors, "#{context}: duplicate source relocation for #{target_id}") if resolved.key?(target_id.to_s)
    end
    next unless errors.length == error_count

    targets.each { |target_id| resolved[target_id.to_s] = current_path }
  end
  resolved
end

# A vault file that backed derivations and was deliberately deleted. The
# receipt binds the exact absent path and hash, needs authorization, a reason
# and evidence naming it, and sends its derivations to review rather than
# passing them: the memories stay, their evidence is gone. A file still in the
# vault cannot be retired, so this cannot cover a changed file.
# (2020-01-02: Example Owner removed 目录册 V3_1 and kept only V8.)
def validated_retired_sources(receipts, by_id, vault_root, errors, ledger_path, relocated)
  retired = {}
  receipts.select { |receipt| receipt["receipt_type"] == "source_retired" }.each do |receipt|
    context = "#{ledger_path}:#{receipt["line_number"]}"
    error_count = errors.length
    retired_path = receipt["source_path"].to_s
    sha = receipt["source_sha256"].to_s
    unless receipt["state"] == "applied" && receipt["source_root"] == "vault"
      add_error(errors, "#{context}: invalid source retirement contract")
    end
    old_path = contained_path(vault_root, retired_path)
    if !old_path || File.exist?(old_path) || File.symlink?(old_path)
      add_error(errors, "#{context}: retirement requires the source to be absent from the vault")
    end
    add_error(errors, "#{context}: source retirement lacks authorization") if receipt["authorized_by"].to_s.strip.empty?
    add_error(errors, "#{context}: source retirement lacks a reason") if receipt["reason"].to_s.strip.empty?
    evidence_ref = receipt["evidence_ref"].to_s
    evidence_sha = receipt["evidence_sha256"].to_s
    proof_valid = begin
      Pathname.new(evidence_ref).absolute? && File.file?(evidence_ref) &&
        File.realpath(evidence_ref) == File.expand_path(evidence_ref) &&
        evidence_sha.match?(SHA_RE) && Digest::SHA256.file(evidence_ref).hexdigest == evidence_sha &&
        Array(JSON.parse(File.read(evidence_ref, encoding: "UTF-8"))["retirements"]).any? do |entry|
          entry.is_a?(Hash) && entry["source_root"] == "vault" &&
            entry["path"] == retired_path && entry["sha256"] == sha
        end
    rescue StandardError
      false
    end
    add_error(errors, "#{context}: retirement evidence does not bind this path and hash") unless proof_valid
    targets = receipt["resolved_receipt_ids"]
    unless targets.is_a?(Array) && !targets.empty? && targets.uniq.length == targets.length
      add_error(errors, "#{context}: retirement requires unique derivation receipt IDs")
      targets = []
    end
    targets.each do |target_id|
      target = by_id[target_id.to_s]
      unless target && target["receipt_type"] == "derivation" && target["source_root"] == "vault" &&
          target["source_path"] == retired_path && target["source_sha256"] == sha
        add_error(errors, "#{context}: retirement target does not match the retired source and hash")
      end
      add_error(errors, "#{context}: source both relocated and retired for #{target_id}") if relocated.key?(target_id.to_s)
      add_error(errors, "#{context}: duplicate source retirement for #{target_id}") if retired.key?(target_id.to_s)
    end
    next unless errors.length == error_count

    targets.each { |target_id| retired[target_id.to_s] = retired_path }
  end
  retired
end

def validate_derivation_receipts(path, vault_root, records_by_id, errors, coordination_points)
  unless File.file?(path) && !File.symlink?(path)
    add_error(errors, "#{path}: immutable derivation receipt ledger is missing or unsafe")
    return [0, {}]
  end

  receipts = []
  File.readlines(path, chomp: true, encoding: "UTF-8").reject(&:empty?).each_with_index do |line, index|
    receipt = JSON.parse(line)
    unless receipt.is_a?(Hash)
      add_error(errors, "#{path}:#{index + 1}: receipt must be an object")
      next
    end
    receipt["line_number"] = index + 1
    receipts << receipt
  rescue JSON::ParserError => e
    add_error(errors, "#{path}:#{index + 1}: invalid JSON #{e.message}")
  end
  by_id = {}
  receipts.each do |receipt|
    receipt_id = receipt["receipt_id"].to_s
    context = "#{path}:#{receipt["line_number"]}"
    add_error(errors, "#{context}: invalid receipt_id") unless receipt_id.match?(CONTENT_SHA_RE)
    add_error(errors, "#{context}: duplicate receipt_id #{receipt_id}") if by_id.key?(receipt_id)
    by_id[receipt_id] = receipt
    add_error(errors, "#{context}: invalid source_sha256") unless receipt["source_sha256"].to_s.match?(SHA_RE)
    case receipt["receipt_type"]
    when "derivation"
      memory_ids = receipt["memory_ids"]
      unless memory_ids.is_a?(Array) && memory_ids.all? { |memory_id| memory_id.to_s.match?(MEMORY_ID_RE) }
        add_error(errors, "#{context}: derivation memory_ids must be an ID array")
      else
        memory_ids.each do |memory_id|
          add_error(errors, "#{context}: unresolved derived memory #{memory_id}") unless records_by_id.key?(memory_id.to_s)
        end
      end
      add_error(errors, "#{context}: empty derivation conclusion") if receipt["conclusion"].to_s.strip.empty?
      if receipt["disposition"] == "extracted" && Array(memory_ids).empty? && receipt["state"] != "unlinked_review_required"
        add_error(errors, "#{context}: unlinked extraction must require review")
      end
    when "source_relocation"
      # Checked below against the immutable derivation, proof and current bytes.
    when "stale_derivation"
      add_error(errors, "#{context}: stale receipt must be pending_review") unless receipt["state"] == "pending_review"
    when "source_retired"
      # Checked below against the missing file, its derivations and the proof.
    when "administrative_source_change"
      add_error(errors, "#{context}: administrative source change must be applied") unless receipt["state"] == "applied"
      unless receipt["change_kind"] == "path_reference_only"
        add_error(errors, "#{context}: unsupported administrative source change kind")
      end
      add_error(errors, "#{context}: invalid previous_source_sha256") unless receipt["previous_source_sha256"].to_s.match?(SHA_RE)
      resolved_ids = receipt["resolved_receipt_ids"]
      unless resolved_ids.is_a?(Array) && !resolved_ids.empty? && resolved_ids.all? { |receipt_id| receipt_id.to_s.match?(CONTENT_SHA_RE) }
        add_error(errors, "#{context}: resolved_receipt_ids must contain receipt IDs")
      end
      changed_fields = receipt["changed_fields"]
      unless changed_fields.is_a?(Array) && !changed_fields.empty? && changed_fields.all? { |field| !field.to_s.strip.empty? }
        add_error(errors, "#{context}: changed_fields must be a non-empty string array")
      end
      add_error(errors, "#{context}: administrative source change lacks authorization") if receipt["authorized_by"].to_s.strip.empty?
      evidence_ref = receipt["evidence_ref"].to_s
      evidence_valid = begin
        Pathname.new(evidence_ref).absolute? && File.file?(evidence_ref) && !File.symlink?(evidence_ref)
      rescue StandardError
        false
      end
      add_error(errors, "#{context}: administrative source change evidence is missing or unsafe") unless evidence_valid
    else
      add_error(errors, "#{context}: invalid receipt_type #{receipt["receipt_type"].inspect}")
    end
  end
  source_relocations = validated_source_relocations(receipts, by_id, vault_root, errors, path)
  retired_sources = validated_retired_sources(receipts, by_id, vault_root, errors, path, source_relocations)
  receipts.select { |receipt| receipt["receipt_type"] == "stale_derivation" }.each do |receipt|
    target = receipt["stale_receipt_id"].to_s
    add_error(errors, "#{path}:#{receipt["line_number"]}: unresolved stale_receipt_id #{target}") unless by_id[target]
  end
  stale_targets = receipts.select { |receipt| receipt["receipt_type"] == "stale_derivation" }
    .map { |receipt| receipt["stale_receipt_id"].to_s }.to_h { |receipt_id| [receipt_id, true] }
  administrative_resolutions = {}
  receipts.select { |receipt| receipt["receipt_type"] == "administrative_source_change" }.each do |receipt|
    context = "#{path}:#{receipt["line_number"]}"
    source_path = if receipt["source_root"] == "vault"
                    real_contained_path(vault_root, receipt["source_path"].to_s)
                  end
    current_sha = source_path && File.file?(source_path) ? Digest::SHA256.file(source_path).hexdigest : nil
    add_error(errors, "#{context}: administrative source is missing or unsafe") unless source_path && File.file?(source_path)
    add_error(errors, "#{context}: administrative source_sha256 does not match current file") unless current_sha == receipt["source_sha256"]
    Array(receipt["resolved_receipt_ids"]).each do |target_id|
      target = by_id[target_id.to_s]
      unless target && target["receipt_type"] == "derivation"
        add_error(errors, "#{context}: administrative resolution target is not a derivation #{target_id}")
        next
      end
      unless target["source_root"] == receipt["source_root"] && target["source_path"] == receipt["source_path"]
        add_error(errors, "#{context}: administrative resolution source does not match #{target_id}")
        next
      end
      unless target["source_sha256"] == receipt["previous_source_sha256"]
        add_error(errors, "#{context}: administrative previous hash does not match #{target_id}")
        next
      end
      if administrative_resolutions.key?(target_id.to_s)
        add_error(errors, "#{context}: duplicate administrative resolution for #{target_id}")
      else
        administrative_resolutions[target_id.to_s] = receipt
      end
    end
  end
  receipts.select { |receipt| receipt["receipt_type"] == "derivation" }.each do |receipt|
    if retired_sources.key?(receipt["receipt_id"].to_s)
      coordination_points << "Derived source was retired; its memories await review: #{receipt["source_path"]}."
      next
    end
    source_path = if receipt["source_root"] == "vault"
                    real_contained_path(vault_root, receipt["source_path"].to_s)
                  elsif receipt["source_root"] == "local" && Pathname.new(receipt["source_path"].to_s).absolute?
                    File.file?(receipt["source_path"].to_s) && !File.symlink?(receipt["source_path"].to_s) ? File.realpath(receipt["source_path"].to_s) : nil
                  end
    source_path ||= source_relocations[receipt["receipt_id"].to_s]
    unless source_path && File.file?(source_path)
      add_error(errors, "#{path}:#{receipt["line_number"]}: missing or unsafe derivation source")
      next
    end
    next if Digest::SHA256.file(source_path).hexdigest == receipt["source_sha256"]
    next if annotation_managed_source_hashes(source_path).include?(receipt["source_sha256"].to_s)
    next if administrative_resolutions.key?(receipt["receipt_id"].to_s)

    unless stale_targets[receipt["receipt_id"].to_s]
      add_error(errors, "#{path}:#{receipt["line_number"]}: changed derivation source lacks a pending stale receipt")
    end
    coordination_points << "Derived source changed and is pending review: #{receipt["source_path"]}."
  rescue StandardError => e
    add_error(errors, "#{path}:#{receipt["line_number"]}: derivation source validation error #{e.class}: #{e.message}")
  end
  [receipts.length, count_values(receipts.map { |receipt| receipt["receipt_type"] })]
rescue StandardError => e
  add_error(errors, "#{path}: derivation receipt validation error #{e.class}: #{e.message}")
  [0, {}]
end

def validate_destination_reference(reference, storage_class, memory_root, external_root, errors, context)
  prefix, relative = reference.to_s.split(":", 2)
  unless prefix == storage_class && %w[obsidian external quarantine].include?(prefix)
    add_error(errors, "#{context}: destination_ref class mismatch #{reference.inspect}")
    return nil
  end
  unless safe_relative?(relative)
    add_error(errors, "#{context}: unsafe destination_ref #{reference.inspect}")
    return nil
  end
  required_prefix = {
    "obsidian" => "91_归档", "external" => "external_payloads",
    "quarantine" => "quarantine/items"
  }.fetch(storage_class)
  unless relative == required_prefix || relative.start_with?("#{required_prefix}/")
    add_error(errors, "#{context}: destination_ref must start with #{required_prefix}/")
    return nil
  end
  storage_root = storage_class == "obsidian" ? memory_root : external_root
  contained_path(storage_root, relative)
end

def validate_watch_destination_reference(reference, external_root, errors, context,
                                         expected_source_id: nil, expected_entry_id: nil,
                                         expected_version: nil, allowed_prefixes: %w[external quarantine])
  prefix, relative = reference.to_s.split(":", 2)
  unless allowed_prefixes.include?(prefix)
    add_error(errors, "#{context}: watcher destination_ref has invalid class #{reference.inspect}")
    return nil
  end
  unless safe_relative?(relative)
    add_error(errors, "#{context}: unsafe watcher destination_ref #{reference.inspect}")
    return nil
  end

  expected_root = prefix == "external" ? "win11-folder-watch/versions" : "quarantine/win11-folder-watch"
  parts = relative.split("/")
  valid_shape = parts.length == 6 && parts.first(2).join("/") == expected_root &&
                parts[2].match?(/\A[A-Za-z0-9._-]{1,128}\z/) && parts[3].match?(UUID_RE) &&
                parts[4].match?(/\Av\d{6}\z/) && parts[5].match?(/\Acontent(?:\.[a-z0-9]{1,16})?\z/)
  unless valid_shape
    add_error(errors, "#{context}: watcher destination_ref has invalid layout #{reference.inspect}")
    return nil
  end
  add_error(errors, "#{context}: watcher destination_ref source_id mismatch") if expected_source_id && parts[2] != expected_source_id
  add_error(errors, "#{context}: watcher destination_ref source_entry_id mismatch") if expected_entry_id && parts[3] != expected_entry_id
  if expected_version && parts[4] != format("v%06d", expected_version)
    add_error(errors, "#{context}: watcher destination_ref version mismatch")
  end
  contained_path(external_root, relative)
end

def table_primary_key(database, table)
  database.execute("PRAGMA table_info(#{table})")
    .select { |row| (row["pk"] || row[5]).to_i.positive? }
    .sort_by { |row| (row["pk"] || row[5]).to_i }
    .map { |row| row["name"] || row[1] }
end

def validate_bridge_database(db_path, memory_root, external_root, win_records, errors, coordination_points)
  stats = {
    "user_version" => nil, "journal_mode" => nil, "events" => 0, "files" => 0,
    "watch_sources" => 0, "watch_entries" => 0, "watch_versions" => 0,
    "source_events" => 0, "maintenance_state" => 0,
    "index_dirty_generation" => nil, "index_applied_generation" => nil,
    "index_last_refreshed_at" => nil, "index_last_error" => nil,
    "index_state" => "not_initialized"
  }
  unless SQLITE_AVAILABLE
    add_error(errors, "#{db_path}: sqlite3 Ruby library unavailable")
    return stats
  end
  unless File.file?(db_path) && !File.symlink?(db_path)
    add_error(errors, "#{db_path}: required bridge database is missing or unsafe")
    return stats
  end

  database = SQLite3::Database.new(db_path, readonly: true)
  database.results_as_hash = true
  database.busy_timeout(5000)
  stats["user_version"] = database.get_first_value("PRAGMA user_version").to_i
  stats["journal_mode"] = database.get_first_value("PRAGMA journal_mode").to_s
  integrity = database.get_first_value("PRAGMA integrity_check")
  add_error(errors, "#{db_path}: SQLite integrity_check returned #{integrity.inspect}") unless integrity == "ok"
  unless [2, 3].include?(stats["user_version"])
    add_error(errors, "#{db_path}: unsupported user_version #{stats["user_version"]}; expected 2 or 3")
  end
  if stats["user_version"] == 2
    coordination_points << "SQLite v2 remains compatible for quick capture; install the watcher core migration before activating folder watch."
  elsif stats["user_version"] == 3 && stats["journal_mode"].downcase != "wal"
    add_error(errors, "#{db_path}: SQLite v3 requires journal_mode=WAL")
  end

  tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").map { |row| row["name"] || row[0] }
  %w[events files].each { |table| add_error(errors, "#{db_path}: missing table #{table}") unless tables.include?(table) }
  return stats unless %w[events files].all? { |table| tables.include?(table) }

  event_columns = database.execute("PRAGMA table_info(events)")
  file_columns = database.execute("PRAGMA table_info(files)")
  event_names = event_columns.map { |row| row["name"] || row[1] }
  file_names = file_columns.map { |row| row["name"] || row[1] }
  required_events = %w[event_id bundle_sha256 source_device created_at processed_at status destination_relative obsidian_count external_count quarantine_count]
  required_files = %w[sha256 size_bytes destination_relative first_event_id stored_at storage_class destination_ref]
  missing_event_columns = required_events - event_names
  missing_file_columns = required_files - file_names
  add_error(errors, "#{db_path}: events missing columns #{missing_event_columns.join(",")}") unless missing_event_columns.empty?
  add_error(errors, "#{db_path}: files missing columns #{missing_file_columns.join(",")}") unless missing_file_columns.empty?
  primary_key = table_primary_key(database, "files")
  add_error(errors, "#{db_path}: files primary key must be sha256,storage_class") unless primary_key == %w[sha256 storage_class]
  return stats unless missing_event_columns.empty? && missing_file_columns.empty?

  events = database.execute("SELECT * FROM events")
  stats["events"] = events.length
  events_by_id = {}
  events.each do |event|
    event_id = event["event_id"].to_s
    context = "#{db_path}: event #{event_id}"
    unless event_id.match?(UUID_RE)
      add_error(errors, "#{context}: invalid event_id")
    end
    add_error(errors, "#{context}: duplicate event_id") if events_by_id.key?(event_id)
    events_by_id[event_id] = event
    add_error(errors, "#{context}: invalid bundle_sha256") unless event["bundle_sha256"].to_s.match?(SHA_RE)
    relative = event["destination_relative"].to_s
    destination = contained_path(memory_root, relative)
    unless destination && relative.start_with?("91_归档/") && File.directory?(destination) && !File.symlink?(destination)
      add_error(errors, "#{context}: unsafe or missing event destination #{relative.inspect}")
    end
    %w[obsidian_count external_count quarantine_count].each do |field|
      value = event[field]
      add_error(errors, "#{context}: invalid #{field}") unless value.is_a?(Integer) && value >= 0
    end
  end

  source_events_by_id = {}
  source_event_payloads = {}
  latest_entry_event = {}
  latest_note_event = {}
  latest_source_event = {}
  watch_sources_by_id = {}
  watch_sources_by_uuid = {}
  watch_versions_by_key = {}
  watch_entries_by_entry_id = Hash.new { |hash, key| hash[key] = [] }
  watch_contract_ready = false

  if stats["user_version"] == 3
    missing_watch_tables = WATCH_TABLE_COLUMNS.keys - tables
    unless missing_watch_tables.empty?
      add_error(errors, "#{db_path}: SQLite v3 missing watcher tables #{missing_watch_tables.join(",")}")
      coordination_points << "Receiver/core schema differs: add or rename watcher tables to #{WATCH_TABLE_COLUMNS.keys.join(", ")}."
    end
    watch_contract_ready = missing_watch_tables.empty?
    if watch_contract_ready
      WATCH_TABLE_COLUMNS.each do |table, required_columns|
        names = database.execute("PRAGMA table_info(#{table})").map { |row| row["name"] || row[1] }
        missing = required_columns - names
        next if missing.empty?

        add_error(errors, "#{db_path}: #{table} missing columns #{missing.join(",")}")
        coordination_points << "Receiver/core schema differs: #{table} must expose #{required_columns.join(", ")}."
        watch_contract_ready = false
      end
    end
    if watch_contract_ready
      expected_primary_keys = {
        "watch_sources" => %w[source_id],
        "watch_entries" => %w[source_id source_entry_id],
        "watch_versions" => %w[source_id source_entry_id version_number],
        "source_events" => %w[event_id],
        "maintenance_state" => %w[key]
      }
      expected_primary_keys.each do |table, expected|
        actual = table_primary_key(database, table)
        add_error(errors, "#{db_path}: #{table} primary key must be #{expected.join(",")}") unless actual == expected
      end
      index_matches = database.execute("PRAGMA index_list(watch_entries)").any? do |index|
        index_name = index["name"] || index[1]
        database.execute("PRAGMA index_info(#{index_name})").map { |row| row["name"] || row[2] } == %w[source_id path_key active]
      end
      add_error(errors, "#{db_path}: watch_entries requires index (source_id,path_key,active)") unless index_matches
    end
  end

  if watch_contract_ready
    watch_sources = database.execute("SELECT * FROM watch_sources")
    stats["watch_sources"] = watch_sources.length
    watch_sources.each do |row|
      source_id = row["source_id"].to_s
      source_uuid = row["source_uuid"].to_s
      context = "#{db_path}: watch source #{source_id.inspect}"
      add_error(errors, "#{context}: invalid source_id") unless source_id.match?(/\A[A-Za-z0-9._-]{1,128}\z/)
      add_error(errors, "#{context}: invalid source_uuid") unless source_uuid.match?(UUID_RE)
      add_error(errors, "#{context}: source_system must be #{WINDOWS_FOLDER_WATCH}") unless row["source_system"] == WINDOWS_FOLDER_WATCH
      add_error(errors, "#{context}: invalid last_event_id") unless row["last_event_id"].to_s.match?(UUID_RE)
      unless row["active_entries"].is_a?(Integer) && row["active_entries"] >= 0
        add_error(errors, "#{context}: invalid active_entries")
      end
      add_error(errors, "#{context}: duplicate source_id") if watch_sources_by_id.key?(source_id)
      add_error(errors, "#{context}: duplicate source_uuid") if watch_sources_by_uuid.key?(source_uuid)
      watch_sources_by_id[source_id] = row
      watch_sources_by_uuid[source_uuid] = row
    end

    source_events = database.execute("SELECT rowid AS _rowid, * FROM source_events ORDER BY rowid")
    stats["source_events"] = source_events.length
    source_events.each do |row|
      event_id = row["event_id"].to_s
      context = "#{db_path}: source event #{event_id}"
      add_error(errors, "#{context}: invalid event_id") unless event_id.match?(UUID_RE)
      add_error(errors, "#{context}: duplicate event_id") if source_events_by_id.key?(event_id)
      source_events_by_id[event_id] = row
      add_error(errors, "#{context}: invalid bundle_sha256") unless row["bundle_sha256"].to_s.match?(SHA_RE)
      add_error(errors, "#{context}: source_system must be #{WINDOWS_FOLDER_WATCH}") unless row["source_system"] == WINDOWS_FOLDER_WATCH
      add_error(errors, "#{context}: missing watch source #{row["source_id"]}") unless watch_sources_by_id.key?(row["source_id"].to_s)
      add_error(errors, "#{context}: invalid source_entry_id") unless row["source_entry_id"].to_s.match?(UUID_RE)
      operation = row["operation"].to_s
      add_error(errors, "#{context}: invalid operation #{operation.inspect}") unless WATCH_OPERATIONS.include?(operation)
      add_error(errors, "#{context}: unsafe source_relative_path") unless safe_relative?(row["source_relative_path"])
      if !row["previous_source_relative_path"].to_s.empty? && !safe_relative?(row["previous_source_relative_path"])
        add_error(errors, "#{context}: unsafe previous_source_relative_path")
      end
      %w[previous_sha256 current_sha256].each do |field|
        value = row[field].to_s
        add_error(errors, "#{context}: invalid #{field}") unless value.empty? || value.match?(SHA_RE)
      end
      begin
        payload = JSON.parse(row["items_json"].to_s)
        unless payload.is_a?(Hash) && payload.keys.sort == %w[capture_error items source_size_bytes]
          add_error(errors, "#{context}: items_json must contain items,capture_error,source_size_bytes")
          payload = {}
        end
        items = payload["items"]
        capture_error = payload["capture_error"]
        source_size = payload["source_size_bytes"]
        add_error(errors, "#{context}: items_json items must be an array") unless items.is_a?(Array)
        unless source_size.is_a?(Integer) && source_size >= 0
          add_error(errors, "#{context}: items_json source_size_bytes must be a non-negative integer")
        end
        unless capture_error.nil? || capture_error.to_s.match?(/\A[a-z0-9_]{1,64}\z/)
          add_error(errors, "#{context}: items_json capture_error is invalid")
        end
        if items.is_a?(Array)
          items.each do |item|
            valid_item = item.is_a?(Hash) && item.keys.sort == %w[relative_path sha256 size_bytes] &&
                         item["relative_path"] == "payload/content" &&
                         item["size_bytes"].is_a?(Integer) && item["size_bytes"] >= 0 &&
                         item["sha256"].to_s.match?(SHA_RE) &&
                         item["size_bytes"] == source_size && item["sha256"] == row["current_sha256"]
            add_error(errors, "#{context}: invalid payload item") unless valid_item
          end
        end
        status = row["status"].to_s
        metadata_only = status == "metadata_only"
        capture_failure = !capture_error.nil?
        valid_status = %w[stored quarantined tombstoned renamed metadata_only capture_recovered].include?(status) ||
                       status.match?(/\Acapture_error:[a-z0-9_]{1,64}\z/)
        add_error(errors, "#{context}: invalid status #{status.inspect}") unless valid_status
        if %w[deleted renamed].include?(operation)
          add_error(errors, "#{context}: #{operation} requires items=[]") unless items == []
          add_error(errors, "#{context}: #{operation} cannot carry capture_error") if capture_failure
        elsif metadata_only
          add_error(errors, "#{context}: metadata_only must use operation=modified and items=[]") unless operation == "modified" && items == []
          add_error(errors, "#{context}: metadata_only cannot carry capture_error") if capture_failure
        elsif capture_failure
          add_error(errors, "#{context}: capture failure requires items=[]") unless items == []
          add_error(errors, "#{context}: capture failure status mismatch") unless status == "capture_error:#{capture_error}"
        elsif %w[baseline added modified].include?(operation)
          add_error(errors, "#{context}: #{operation} requires exactly one payload item") unless items.is_a?(Array) && items.length == 1
        end
        previous_path = row["previous_source_relative_path"]
        previous_sha = row["previous_sha256"]
        current_sha = row["current_sha256"]
        if %w[baseline added].include?(operation)
          unless previous_path.nil? && previous_sha.nil? && ((capture_failure && current_sha.nil?) || (!capture_failure && current_sha.to_s.match?(SHA_RE)))
            add_error(errors, "#{context}: invalid #{operation} transition")
          end
        elsif operation == "modified"
          add_error(errors, "#{context}: modified must retain source_relative_path") unless previous_path == row["source_relative_path"]
          if metadata_only
            add_error(errors, "#{context}: metadata_only must retain SHA-256") unless previous_sha.to_s.match?(SHA_RE) && previous_sha == current_sha
          elsif status == "capture_recovered"
            add_error(errors, "#{context}: capture_recovered must retain SHA-256") unless previous_sha.to_s.match?(SHA_RE) && previous_sha == current_sha
          elsif capture_failure
            add_error(errors, "#{context}: capture failure modified must clear current_sha256") unless current_sha.nil?
          else
            add_error(errors, "#{context}: content modified must change SHA-256") unless current_sha.to_s.match?(SHA_RE) && previous_sha != current_sha
          end
        elsif operation == "deleted"
          uncaptured = MemoryValidationContracts.deleted_before_capture?(
            row, latest_entry_event[[row["source_id"].to_s, row["source_entry_id"].to_s]], source_events_by_id
          )
          if uncaptured
            stats["uncaptured_tombstones"] = stats.fetch("uncaptured_tombstones", 0) + 1
            coordination_points << "A source was deleted before any successful capture; evidence remains unavailable and no content hash was invented."
          end
          unless previous_path == row["source_relative_path"] && (previous_sha.to_s.match?(SHA_RE) || uncaptured) && current_sha.nil? && status == "tombstoned"
            add_error(errors, "#{context}: invalid deleted transition")
          end
        elsif operation == "renamed"
          unless previous_path && previous_path != row["source_relative_path"] && previous_sha.to_s.match?(SHA_RE) &&
                 previous_sha == current_sha && status == "renamed"
            add_error(errors, "#{context}: invalid renamed transition")
          end
        end
        source_event_payloads[event_id] = payload
      rescue JSON::ParserError
        add_error(errors, "#{context}: invalid items_json")
      end
      add_error(errors, "#{context}: empty status") if row["status"].to_s.empty?
      entry_key = [row["source_id"].to_s, row["source_entry_id"].to_s]
      latest_entry_event[entry_key] = row
      latest_note_event[entry_key] = row unless row["status"].to_s == "metadata_only"
      latest_source_event[row["source_id"].to_s] = row
    end

    watch_versions = database.execute("SELECT * FROM watch_versions")
    stats["watch_versions"] = watch_versions.length
    watch_versions.each do |row|
      source_id = row["source_id"].to_s
      entry_id = row["source_entry_id"].to_s
      version = row["version_number"].to_i
      context = "#{db_path}: watch version #{source_id}/#{entry_id}/#{version}"
      add_error(errors, "#{context}: missing watch source") unless watch_sources_by_id.key?(source_id)
      add_error(errors, "#{context}: invalid source_entry_id") unless entry_id.match?(UUID_RE)
      add_error(errors, "#{context}: invalid version_number") unless row["version_number"].is_a?(Integer) && version.positive?
      add_error(errors, "#{context}: invalid event_id") unless row["event_id"].to_s.match?(UUID_RE)
      add_error(errors, "#{context}: source event is missing") unless source_events_by_id.key?(row["event_id"].to_s)
      add_error(errors, "#{context}: invalid sha256") unless row["sha256"].to_s.match?(SHA_RE)
      add_error(errors, "#{context}: invalid size_bytes") unless row["size_bytes"].is_a?(Integer) && row["size_bytes"] >= 0
      add_error(errors, "#{context}: unsafe source_relative_path") unless safe_relative?(row["source_relative_path"])
      unless %w[obsidian external quarantine].include?(row["storage_class"].to_s)
        add_error(errors, "#{context}: invalid storage_class #{row["storage_class"].inspect}")
      end
      destination = validate_watch_destination_reference(
        row["destination_ref"], external_root, errors, context,
        expected_source_id: source_id, expected_entry_id: entry_id, expected_version: version,
        allowed_prefixes: %w[external]
      )
      unless destination && File.file?(destination) && !File.symlink?(destination)
        add_error(errors, "#{context}: immutable version file is missing or unsafe")
      else
        add_error(errors, "#{context}: stale version hash") unless Digest::SHA256.file(destination).hexdigest == row["sha256"].to_s
        add_error(errors, "#{context}: stale version size") unless File.size(destination) == row["size_bytes"].to_i
      end
      watch_versions_by_key[[source_id, entry_id, version]] = row
    rescue StandardError => e
      add_error(errors, "#{context}: validation error #{e.class}: #{e.message}")
    end

    watch_entries = database.execute("SELECT * FROM watch_entries")
    stats["watch_entries"] = watch_entries.length
    active_counts = Hash.new(0)
    watch_entries.each do |row|
      source_id = row["source_id"].to_s
      entry_id = row["source_entry_id"].to_s
      context = "#{db_path}: watch entry #{source_id}/#{entry_id}"
      watch_entries_by_entry_id[entry_id] << row
      add_error(errors, "#{context}: missing watch source") unless watch_sources_by_id.key?(source_id)
      add_error(errors, "#{context}: invalid source_entry_id") unless entry_id.match?(UUID_RE)
      add_error(errors, "#{context}: unsafe current_relative_path") unless safe_relative?(row["current_relative_path"])
      add_error(errors, "#{context}: empty path_key") if row["path_key"].to_s.empty?
      active = row["active"].to_i
      add_error(errors, "#{context}: active must be 0 or 1") unless row["active"].is_a?(Integer) && [0, 1].include?(active)
      active_counts[source_id] += 1 if active == 1
      version = row["version_number"].to_i
      add_error(errors, "#{context}: invalid version_number") unless row["version_number"].is_a?(Integer) && version >= 0
      current_hash = row["current_sha256"].to_s
      add_error(errors, "#{context}: invalid current_sha256") unless current_hash.empty? || current_hash.match?(SHA_RE)
      if active == 0
        add_error(errors, "#{context}: tombstone must clear current_sha256") unless current_hash.empty?
        add_error(errors, "#{context}: tombstone requires deleted_at") if row["deleted_at"].to_s.empty?
      end
      if version.positive?
        destination = validate_watch_destination_reference(
          row["version_ref"], external_root, errors, context,
          expected_source_id: source_id, expected_entry_id: entry_id, expected_version: version,
          allowed_prefixes: %w[external]
        )
        add_error(errors, "#{context}: latest immutable version is missing") unless destination && File.file?(destination) && !File.symlink?(destination)
        add_error(errors, "#{context}: latest watch_versions row is missing") unless watch_versions_by_key.key?([source_id, entry_id, version])
      end
      mirror = row["mirror_relative"].to_s
      unless mirror.empty?
        expected_mirror = "91_归档/Win11镜像/#{row["current_relative_path"]}"
        add_error(errors, "#{context}: mirror_relative mismatch") unless mirror == expected_mirror
        mirror_path = contained_path(memory_root, mirror)
        if active == 1 && (!mirror_path || !File.file?(mirror_path) || File.symlink?(mirror_path))
          add_error(errors, "#{context}: current mirror is missing or unsafe")
        end
      end
      event = source_events_by_id[row["last_event_id"].to_s]
      if event.nil?
        add_error(errors, "#{context}: last_event_id is missing")
      elsif event["source_id"].to_s != source_id || event["source_entry_id"].to_s != entry_id
        add_error(errors, "#{context}: last_event_id points to another source entry")
      end
      latest = latest_entry_event[[source_id, entry_id]]
      if latest && row["last_event_id"].to_s != latest["event_id"].to_s
        add_error(errors, "#{context}: last_event_id is not the latest source event")
      end
    rescue StandardError => e
      add_error(errors, "#{context}: validation error #{e.class}: #{e.message}")
    end
    watch_sources_by_id.each do |source_id, source|
      unless source["active_entries"].to_i == active_counts[source_id]
        add_error(errors, "#{db_path}: watch source #{source_id}: active_entries count mismatch")
      end
      latest = latest_source_event[source_id]
      if latest.nil?
        add_error(errors, "#{db_path}: watch source #{source_id}: has no source event")
      elsif source["last_event_id"].to_s != latest["event_id"].to_s
        add_error(errors, "#{db_path}: watch source #{source_id}: last_event_id is not the latest source event")
      end
    end

    maintenance_rows = database.execute("SELECT key,value,updated_at FROM maintenance_state")
    stats["maintenance_state"] = maintenance_rows.length
    maintenance = maintenance_rows.to_h { |row| [row["key"].to_s, row] }
    maintenance_rows.each do |row|
      add_error(errors, "#{db_path}: maintenance_state #{row["key"].inspect} has empty updated_at") if row["updated_at"].to_s.empty?
    end
    required_maintenance = %w[index_dirty_generation index_applied_generation index_last_refreshed_at index_last_error]
    if latest_note_event.any?
      missing = required_maintenance - maintenance.keys
      add_error(errors, "#{db_path}: maintenance_state missing index refresh keys #{missing.join(",")}") unless missing.empty?
    end
    dirty = nil
    applied = nil
    begin
      dirty = Integer(maintenance.fetch("index_dirty_generation").fetch("value"), 10) if maintenance.key?("index_dirty_generation")
    rescue ArgumentError, TypeError
      add_error(errors, "#{db_path}: invalid index_dirty_generation")
    end
    begin
      applied = Integer(maintenance.fetch("index_applied_generation").fetch("value"), 10) if maintenance.key?("index_applied_generation")
    rescue ArgumentError, TypeError
      add_error(errors, "#{db_path}: invalid index_applied_generation")
    end
    add_error(errors, "#{db_path}: index generations must be non-negative") if [dirty, applied].compact.any?(&:negative?)
    stats["index_dirty_generation"] = dirty
    stats["index_applied_generation"] = applied
    stats["index_last_refreshed_at"] = maintenance.dig("index_last_refreshed_at", "value")
    stats["index_last_error"] = maintenance.dig("index_last_error", "value")
    if dirty && applied
      stats["index_state"] = dirty == applied ? "up_to_date" : "stale"
      add_error(errors, "#{db_path}: memory indexes are stale (dirty=#{dirty}, applied=#{applied})") unless dirty == applied
      if applied.positive? && stats["index_last_refreshed_at"].to_s.empty?
        add_error(errors, "#{db_path}: applied indexes require index_last_refreshed_at")
      end
    elsif source_events.empty?
      stats["index_state"] = "clean_empty"
    end
    unless stats["index_last_error"].to_s.empty?
      add_error(errors, "#{db_path}: last memory index refresh failed: #{stats["index_last_error"]}")
    end
  elsif win_records.any? { |record| record.fetch("data")["source_system"] == WINDOWS_FOLDER_WATCH }
    add_error(errors, "#{db_path}: folder-watch notes require the compatible SQLite v3 watcher schema")
  end

  files = database.execute("SELECT * FROM files")
  stats["files"] = files.length
  files.each_with_index do |row, index|
    context = "#{db_path}: file row #{index + 1}"
    storage_class = row["storage_class"].to_s
    unless %w[obsidian external quarantine].include?(storage_class)
      add_error(errors, "#{context}: invalid storage_class #{storage_class.inspect}")
      next
    end
    destination = validate_destination_reference(row["destination_ref"], storage_class, memory_root, external_root, errors, context)
    relative = row["destination_ref"].to_s.split(":", 2)[1].to_s
    add_error(errors, "#{context}: destination_relative does not match destination_ref") if row["destination_relative"].to_s != relative
    unless destination && File.file?(destination) && !File.symlink?(destination)
      add_error(errors, "#{context}: destination file is missing or unsafe")
      next
    end
    real_root = File.realpath(storage_class == "obsidian" ? memory_root : external_root)
    real_destination = File.realpath(destination)
    unless real_destination.start_with?("#{real_root}#{File::SEPARATOR}")
      add_error(errors, "#{context}: destination escapes its storage root")
    end
    sha256 = row["sha256"].to_s
    if sha256.match?(SHA_RE)
      add_error(errors, "#{context}: stale destination hash") unless Digest::SHA256.file(destination).hexdigest == sha256
    else
      add_error(errors, "#{context}: invalid sha256")
    end
    add_error(errors, "#{context}: stale destination size") unless row["size_bytes"].to_i == File.size(destination)
    add_error(errors, "#{context}: first_event_id is missing") unless events_by_id.key?(row["first_event_id"].to_s)
  rescue StandardError => e
    add_error(errors, "#{context}: validation error #{e.class}: #{e.message}")
  end

  quick_records = win_records.select { |record| record.fetch("data")["source_system"] == WINDOWS_QUICK_CAPTURE }
  folder_records = win_records.select { |record| record.fetch("data")["source_system"] == WINDOWS_FOLDER_WATCH }
  quick_records.each do |record|
    add_error(errors, "#{record.fetch("path")}: standalone quick-capture review notes must be migrated out of 90_待确认")
  end
  quick_exception_root = File.join(external_root, "business-routing", "quarantine", "quick-capture")
  events_by_id.each do |event_id, event|
    receipt_path = File.join(quick_exception_root, "#{event_id}.json")
    unless File.file?(receipt_path) && !File.symlink?(receipt_path)
      add_error(errors, "#{db_path}: quick-capture event #{event_id} lacks its technical routing exception")
      next
    end
    receipt = JSON.parse(File.read(receipt_path, encoding: "UTF-8"))
    unless receipt["schema_version"] == 1 && receipt["status"] == "system_exception" &&
           receipt["code"] == "quick_capture_unroutable" && receipt["event_id"] == event_id
      add_error(errors, "#{receipt_path}: invalid quick-capture routing exception")
    end
    if receipt["bundle_sha256"].to_s != event["bundle_sha256"].to_s
      add_error(errors, "#{receipt_path}: bundle_sha256 does not match event #{event_id}")
    end
  rescue JSON::ParserError => e
    add_error(errors, "#{receipt_path}: invalid routing exception JSON #{e.message}")
  end
  if Dir.exist?(quick_exception_root)
    Dir.glob(File.join(quick_exception_root, "*.json")).each do |receipt_path|
      event_id = File.basename(receipt_path, ".json")
      add_error(errors, "#{receipt_path}: no quick-capture bridge event #{event_id}") unless events_by_id.key?(event_id)
    end
  end

  if watch_contract_ready
    folder_by_entry = folder_records.to_h do |record|
      match = record.fetch("data")["memory_id"].to_s.match(WIN_ID_RE)
      [match && match[1], record]
    end
    folder_by_entry.delete(nil)
    watch_entries_by_entry_id.each do |entry_id, entries|
      if entries.length != 1
        add_error(errors, "#{db_path}: source_entry_id #{entry_id} is not globally unique")
        next
      end
      entry = entries.first
      source_id = entry["source_id"].to_s
      record = folder_by_entry[entry_id]
      if record.nil?
        add_error(errors, "#{db_path}: watch entry #{source_id}/#{entry_id} lacks its fixed WIN review note")
        next
      end
      data = record.fetch("data")
      expected_ref = "#{WINDOWS_FOLDER_WATCH}:#{source_id}:#{entry_id}"
      add_error(errors, "#{record.fetch("path")}: source_ref does not match watch entry") unless data["source_ref"] == expected_ref
      add_error(errors, "#{record.fetch("path")}: source_entry_id does not match memory_id") unless data["source_entry_id"] == entry_id
      event = latest_note_event[[source_id, entry_id]]
      if event.nil?
        add_error(errors, "#{record.fetch("path")}: watch entry has no note-producing source event")
        next
      end
      unless data["source_event_id"] == event["event_id"]
        add_error(errors, "#{record.fetch("path")}: source_event_id does not match latest note-producing source event")
      end
      if event
        add_error(errors, "#{record.fetch("path")}: source_entry_id does not match latest note-producing event") unless data["source_entry_id"] == event["source_entry_id"]
        add_error(errors, "#{record.fetch("path")}: source_operation does not match latest note-producing event") unless data["source_operation"] == event["operation"]
        expected_hash = "sha256:#{event["bundle_sha256"]}"
        if data.key?("source_bundle_sha256") && data["source_bundle_sha256"].to_s != expected_hash
          add_error(errors, "#{record.fetch("path")}: source_bundle_sha256 does not match latest note-producing event")
        end
      end
    end
    folder_by_entry.each do |entry_id, record|
      add_error(errors, "#{record.fetch("path")}: no watch entry #{entry_id}") unless watch_entries_by_entry_id.key?(entry_id)
    end
  end
  stats
rescue StandardError => e
  add_error(errors, "#{db_path}: database validation error #{e.class}: #{e.message}")
  stats
ensure
  database&.close
end

def validate_semantic_database(db_path, runs_root, errors)
  stats = {
    "quick_check" => nil,
    "journal_mode" => nil,
    "runs" => 0,
    "run_status_counts" => {},
    "applied_transactions" => 0,
    "transaction_journals" => 0
  }
  unless SQLITE_AVAILABLE
    add_error(errors, "#{db_path}: sqlite3 Ruby library unavailable")
    return stats
  end
  unless File.file?(db_path) && !File.symlink?(db_path)
    add_error(errors, "#{db_path}: required semantic database is missing or unsafe")
    return stats
  end
  database = SQLite3::Database.new(db_path, readonly: true)
  database.results_as_hash = true
  database.busy_timeout(5000)
  stats["quick_check"] = database.get_first_value("PRAGMA quick_check").to_s
  stats["journal_mode"] = database.get_first_value("PRAGMA journal_mode").to_s.downcase
  add_error(errors, "#{db_path}: SQLite quick_check returned #{stats["quick_check"].inspect}") unless stats["quick_check"] == "ok"
  add_error(errors, "#{db_path}: semantic database requires WAL") unless stats["journal_mode"] == "wal"
  tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").map { |row| row["name"] || row[0] }
  required_tables = %w[runs items manual_seed id_reservations applied_transactions]
  missing = required_tables - tables
  add_error(errors, "#{db_path}: missing semantic tables #{missing.join(",")}") unless missing.empty?
  return stats unless missing.empty?

  run_columns = database.execute("PRAGMA table_info(runs)").map { |row| row["name"] || row[1] }
  add_error(errors, "#{db_path}: runs missing transaction_id") unless run_columns.include?("transaction_id")
  runs = database.execute("SELECT * FROM runs")
  stats["runs"] = runs.length
  stats["run_status_counts"] = count_values(runs.map { |run| run["status"] })
  process_out, = Open3.capture2("/usr/bin/pgrep", "-f", "win11_semantic_merge.rb (bootstrap|prepare|commit)")
  semantic_active = !process_out.to_s.strip.empty?
  now = Time.now
  runs.each do |run|
    context = "#{db_path}: run #{run["run_id"]}"
    status = run["status"].to_s
    add_error(errors, "#{context}: invalid status #{status.inspect}") unless %w[prepared running completed failed blocked].include?(status)
    if status == "blocked"
      valid_block = MemoryValidationContracts.valid_blocked_run?(run)
      %w[items id_reservations applied_transactions].each do |table|
        valid_block &&= database.get_first_value("SELECT COUNT(*) FROM #{table} WHERE run_id=?", [run["run_id"]]).to_i.zero?
      end
      add_error(errors, "#{context}: invalid blocked run or unexpected writes") unless valid_block
    end
    if status == "failed"
      add_error(errors, "#{context}: failed run requires completed_at") if run["completed_at"].to_s.empty?
      if run["error_code"].to_s.empty? || run["error_message"].to_s.empty?
        add_error(errors, "#{context}: failed run requires error code and message")
      end
    end
    if %w[prepared running].include?(status)
      created = Time.parse(run["created_at"].to_s)
      if !semantic_active && now - created > 7200
        add_error(errors, "#{context}: inactive #{status} run is older than two hours")
      end
    end
    transaction_id = run["transaction_id"].to_s
    add_error(errors, "#{context}: invalid transaction_id") unless transaction_id.empty? || transaction_id.match?(UUID_RE)
  rescue ArgumentError
    add_error(errors, "#{context}: invalid created_at")
  end
  bootstrap = runs.find { |run| run["run_id"] == "manual-v6-bootstrap" }
  unless bootstrap && bootstrap["status"] == "completed"
    add_error(errors, "#{db_path}: current external ledger has not completed the semantic bootstrap")
  end

  applied = database.execute("SELECT * FROM applied_transactions")
  stats["applied_transactions"] = applied.length
  applied_ids = {}
  applied.each do |transaction|
    transaction_id = transaction["transaction_id"].to_s
    context = "#{db_path}: applied transaction #{transaction_id}"
    add_error(errors, "#{context}: invalid transaction_id") unless transaction_id.match?(UUID_RE)
    add_error(errors, "#{context}: duplicate transaction_id") if applied_ids[transaction_id]
    applied_ids[transaction_id] = true
    run = runs.find { |candidate| candidate["run_id"].to_s == transaction["run_id"].to_s }
    add_error(errors, "#{context}: unresolved run_id #{transaction["run_id"]}") unless run
  end

  reservations = database.execute("SELECT * FROM id_reservations")
  reservations.each do |reservation|
    next unless reservation["used_at"].to_s.empty?
    run = runs.find { |candidate| candidate["run_id"].to_s == reservation["run_id"].to_s }
    unless run && %w[prepared running failed].include?(run["status"].to_s)
      add_error(errors, "#{db_path}: unused reservation #{reservation["memory_id"]} is not tied to an unfinished/failed run")
    end
  end

  unless File.directory?(runs_root) && !File.symlink?(runs_root)
    add_error(errors, "#{runs_root}: semantic runs root is missing or unsafe")
    return stats
  end
  Dir.glob(File.join(runs_root, "*", "transaction.json")).sort.each do |journal_path|
    stats["transaction_journals"] += 1
    payload = JSON.parse(File.read(journal_path, encoding: "UTF-8"))
    state = payload["state"].to_s
    unless %w[prepared files_applied db_committed rolled_back].include?(state)
      add_error(errors, "#{journal_path}: invalid transaction state #{state.inspect}")
      next
    end
    transaction_id = payload["transaction_id"].to_s
    add_error(errors, "#{journal_path}: invalid transaction_id") unless transaction_id.match?(UUID_RE)
    if %w[prepared files_applied].include?(state)
      add_error(errors, "#{journal_path}: incomplete file transaction requires startup recovery")
    elsif state == "db_committed" && !applied_ids[transaction_id]
      add_error(errors, "#{journal_path}: db_committed journal lacks applied transaction")
    end
  rescue JSON::ParserError => e
    add_error(errors, "#{journal_path}: invalid transaction journal #{e.message}")
  end
  stats
rescue StandardError => e
  add_error(errors, "#{db_path}: semantic database validation error #{e.class}: #{e.message}")
  stats
ensure
  database&.close
end

def validator_self_test
  passed = 0
  check = lambda do |condition, label|
    raise "self-test failed: #{label}" unless condition
    passed += 1
  end
  Dir.mktmpdir("business-magic-regression-") do |temporary|
    path = File.join(temporary, "fixture.txt")
    ["".b, "ordinary text".b].each do |content|
      File.binwrite(path, content)
      errors = []
      validate_file_magic(path, ".txt", errors)
      check.call(errors.empty?, "empty and ordinary text have no forbidden signature")
    end
    ["MZ".b, "\x7FELF".b, "PK\x03\x04".b, "SQLite format 3\0".b, "#!".b].each do |content|
      File.binwrite(path, content)
      errors = []
      validate_file_magic(path, ".txt", errors)
      check.call(errors.any? { |error| error.include?("forbidden") }, "forbidden signature in text is rejected")
    end
    errors = []
    validate_file_magic(File.join(temporary, "missing.txt"), ".txt", errors)
    check.call(errors.any? { |error| error.include?("signature check error") }, "read failure remains a validation failure")
    File.binwrite(path, "".b)
    errors = []
    validate_ooxml(path, ".docx", errors)
    check.call(!errors.empty?, "empty Office document remains invalid")
  end
  check.call(
    review_metadata_issues(
      "status" => "review_required", "reviewed_at" => "2020-01-02T00:00:00+08:00",
      "reviewed_by" => nil, "review_mode" => nil
    ).length == 2,
    "review_required cannot retain machine review timestamp"
  )
  check.call(
    review_metadata_issues(
      "status" => "active", "reviewed_at" => "2020-01-02T00:00:00+08:00",
      "reviewed_by" => "example", "review_mode" => "human_review"
    ).empty?,
    "attributed human review accepted"
  )
  check.call(
    overview_time_metadata_issues(
      "generated_at" => "2020-01-02T00:00:00+08:00", "content_reviewed_at" => nil,
      "last_event_ingested_at" => "2020-01-02T00:00:00+08:00",
      "last_successful_full_scan_at" => "2020-01-02T00:00:00+08:00"
    ).empty?,
    "split overview timestamps accepted"
  )
  check.call(
    overview_time_metadata_issues(
      "generated_at" => "bad", "content_reviewed_at" => nil,
      "last_event_ingested_at" => nil, "updated_at" => "legacy"
    ).any? { |issue| issue.include?("missing last_successful_full_scan_at") } &&
      overview_time_metadata_issues(
        "generated_at" => "bad", "content_reviewed_at" => nil,
        "last_event_ingested_at" => nil, "updated_at" => "legacy"
      ).any? { |issue| issue.include?("legacy updated_at") || issue.include?("invalid generated_at") },
    "missing, legacy, and invalid overview timestamps rejected"
  )
  status_expected = {
    "memory_notes" => 100, "formal_memory_notes" => 90,
    "formal_current_notes" => 70, "conversation_pending_notes" => 5,
    "quick_capture_notes" => 0, "inline_pending_claims" => 12,
    "external_watch_notes" => 20, "external_watch_pending_notes" => 0,
    "index_dirty_generation" => 7, "index_applied_generation" => 7,
    "index_last_refreshed_at" => "2099-01-01T00:00:00+08:00",
    "index_last_error" => "", "index_state" => "up_to_date"
  }
  status_data = {
    "document_type" => "memory_system_status", "version" => 7.0,
    "generated_at" => "2099-01-01T00:00:00+08:00", "content_reviewed_at" => nil,
    "snapshot_source" => "refresh_memory_indexes",
    "last_event_ingested_at" => "2099-01-01T00:00:00+08:00",
    "last_successful_full_scan_at" => "2099-01-01T00:00:00+08:00",
    "applies_to" => ["project:business-energy"]
  }.merge(status_expected)
  overview_data = {
    "generated_at" => "2099-01-01T00:00:00+08:00",
    "last_event_ingested_at" => "2099-01-01T00:00:00+08:00",
    "last_successful_full_scan_at" => "2099-01-01T00:00:00+08:00"
  }
  check.call(status_snapshot_issues(status_data, overview_data, status_expected).empty?, "generated status snapshot accepted")
  stale_status = status_data.merge("memory_notes" => 99, "generated_at" => "2098-12-31T23:59:59+08:00")
  check.call(
    status_snapshot_issues(stale_status, overview_data, status_expected).any? { |issue| issue.include?("memory_notes") } &&
      status_snapshot_issues(stale_status, overview_data, status_expected).any? { |issue| issue.include?("generated_at") },
    "stale generated status rejected"
  )
  check.call(!supersession_cycles("a" => ["b"], "b" => ["a"]).empty?, "supersession cycle rejected")
  dual = [
    { "data" => { "memory_id" => "a", "series_id" => "x", "status" => "active", "fact_status" => "confirmed", "supersedes" => [] } },
    { "data" => { "memory_id" => "b", "series_id" => "x", "status" => "active", "fact_status" => "confirmed", "supersedes" => [] } }
  ]
  check.call(series_contract_issues(dual).any? { |issue| issue.include?("multiple active") }, "dual current series rejected")
  Dir.mktmpdir("business-validator-") do |temporary|
    root = File.join(temporary, "root")
    outside = File.join(temporary, "outside")
    FileUtils.mkdir_p(root)
    FileUtils.mkdir_p(outside)
    File.write(File.join(root, "inside.txt"), "inside")
    File.write(File.join(outside, "outside.txt"), "outside")
    File.symlink(outside, File.join(root, "escape"))
    FileUtils.mkdir_p(File.join(root, "客户及展会"))
    FileUtils.mkdir_p(File.join(root, "公司资料"))
    File.write(File.join(root, "客户及展会", "inside.md"), "inside")
    base = "受保护正文。\n"
    base_payload = Base64.urlsafe_encode64(
      JSON.generate({
        "schema_version" => 1,
        "base_sha256" => Digest::SHA256.hexdigest(base),
        "base_trailing" => "\n",
        "created_heading" => true,
        "annotation_hashes" => []
      }),
      padding: false
    )
    marked = base.rstrip + "\n\n<!-- business-claim-base:v1 #{base_payload} -->\n## 核验说明\n"
    marked_path = File.join(root, "公司资料", "受治理页面.md")
    File.write(marked_path, marked)
    check.call(
      annotation_managed_source_hashes(marked_path) == [Digest::SHA256.hexdigest(base)],
      "generated claim suffix is accepted"
    )
    File.write(marked_path, marked + "绕过写入的尾部正文。\n")
    suffix_rejected = begin
      annotation_managed_source_hashes(marked_path)
      false
    rescue StandardError => error
      error.message.include?("claim-base untracked suffix")
    end
    check.call(suffix_rejected, "untracked claim suffix is rejected")
    managed = marked.rstrip + "\n\n#{BUSINESS_EXTERNAL_ASSETS_START}\n外置索引\n#{BUSINESS_EXTERNAL_ASSETS_END}\n"
    File.write(marked_path, managed)
    check.call(
      annotation_managed_source_hashes(marked_path) == [Digest::SHA256.hexdigest(base)],
      "bounded legacy external-assets suffix is accepted"
    )
    File.write(File.join(root, "公司资料", "reference.pdf"), "reference")
    check.call(!real_contained_path(root, "inside.txt").nil?, "controlled path accepted")
    check.call(real_contained_path(root, "escape/outside.txt").nil?, "parent symlink escape rejected")
    check.call(real_contained_path(root, "missing.txt").nil?, "missing source path rejected")
    check.call(
      path_like_source_refs("客户及展会/inside.md") == ["客户及展会/inside.md"],
      "single vault source_ref path extracted"
    )
    check.call(
      path_like_source_refs(
        "客户及展会/inside.md；email-message:fixture；补充参考：公司资料/reference.pdf"
      ) == ["客户及展会/inside.md", "公司资料/reference.pdf"],
      "compound source_ref paths extracted independently"
    )
    check.call(
      path_like_source_refs("chatgpt-conversation://fixture；email-message:fixture").empty?,
      "external source identifiers are not treated as local paths"
    )
    missing_refs = path_like_source_refs("客户及展会/missing.md；email-message:fixture")
    check.call(
      missing_refs == ["客户及展会/missing.md"] && resolve_source_ref_path(root, missing_refs.first).nil?,
      "missing path inside compound source_ref is rejected"
    )
    unsafe_refs = path_like_source_refs("客户及展会/../outside.md")
    check.call(
      unsafe_refs == ["客户及展会/../outside.md"] && resolve_source_ref_path(root, unsafe_refs.first).nil?,
      "vault-relative traversal source_ref is rejected"
    )
    check.call(
      path_like_source_refs(File.join(outside, "outside.txt")) == [File.join(outside, "outside.txt")] &&
        !resolve_source_ref_path(root, File.join(outside, "outside.txt")).nil?,
      "existing absolute external source_ref remains supported"
    )
    governed_source = File.join(root, "客户及展会", "governed.md")
    File.write(governed_source, "new reference")
    evidence_ref = File.join(temporary, "administrative-reference-repair.diff")
    File.write(evidence_ref, "reference path only\n")
    old_receipt_id = "sha256:#{"1" * 64}"
    stale_receipt_id = "sha256:#{"2" * 64}"
    administrative_receipt_id = "sha256:#{"3" * 64}"
    old_source_sha = Digest::SHA256.hexdigest("old reference")
    current_source_sha = Digest::SHA256.file(governed_source).hexdigest
    old_derivation = {
      "receipt_id" => old_receipt_id, "receipt_type" => "derivation",
      "source_root" => "vault", "source_path" => "客户及展会/governed.md",
      "source_sha256" => old_source_sha, "disposition" => "extracted",
      "conclusion" => "fixture conclusion", "memory_ids" => [],
      "state" => "unlinked_review_required"
    }
    stale_derivation = {
      "receipt_id" => stale_receipt_id, "receipt_type" => "stale_derivation",
      "source_root" => "vault", "source_path" => "客户及展会/governed.md",
      "source_sha256" => current_source_sha, "stale_receipt_id" => old_receipt_id,
      "state" => "pending_review"
    }
    administrative_resolution = {
      "receipt_id" => administrative_receipt_id,
      "receipt_type" => "administrative_source_change",
      "source_root" => "vault", "source_path" => "客户及展会/governed.md",
      "source_sha256" => current_source_sha, "previous_source_sha256" => old_source_sha,
      "resolved_receipt_ids" => [old_receipt_id], "change_kind" => "path_reference_only",
      "changed_fields" => ["frontmatter.reference"], "state" => "applied",
      "authorized_by" => "self-test", "evidence_ref" => evidence_ref
    }
    unresolved_ledger = File.join(temporary, "unresolved-receipts.jsonl")
    File.write(unresolved_ledger, [old_derivation, stale_derivation].map { |receipt| JSON.generate(receipt) }.join("\n") + "\n")
    unresolved_errors = []
    unresolved_coordination = []
    validate_derivation_receipts(unresolved_ledger, root, {}, unresolved_errors, unresolved_coordination)
    check.call(
      unresolved_errors.empty? && unresolved_coordination.length == 1,
      "ordinary source drift remains a coordination point"
    )
    resolved_ledger = File.join(temporary, "resolved-receipts.jsonl")
    File.write(
      resolved_ledger,
      [old_derivation, stale_derivation, administrative_resolution].map { |receipt| JSON.generate(receipt) }.join("\n") + "\n"
    )
    resolved_errors = []
    resolved_coordination = []
    validate_derivation_receipts(resolved_ledger, root, {}, resolved_errors, resolved_coordination)
    check.call(
      resolved_errors.empty? && resolved_coordination.empty?,
      "authorized path-reference-only source change resolves stale coordination"
    )
    moved_source = File.join(root, "客户及展会", "moved.bin")
    File.write(moved_source, "unchanged source")
    moved_sha = Digest::SHA256.file(moved_source).hexdigest
    move_proof = File.join(temporary, "relocation-proof.json")
    proof = {"relocations" => [{"source_root" => "vault", "from_path" => "客户及展会/original.bin",
      "to_path" => "客户及展会/moved.bin", "sha256" => moved_sha}]}
    File.write(move_proof, JSON.generate(proof))
    move_proof = File.realpath(move_proof)
    move_original = old_derivation.merge("source_path" => "客户及展会/original.bin", "source_sha256" => moved_sha)
    relocation = {
      "receipt_id" => "sha256:#{"4" * 64}", "receipt_type" => "source_relocation",
      "source_root" => "vault", "previous_source_path" => "客户及展会/original.bin",
      "source_path" => "客户及展会/moved.bin", "source_sha256" => moved_sha,
      "state" => "applied", "change_kind" => "path_only", "authorized_by" => "self-test",
      "resolved_receipt_ids" => [old_receipt_id], "evidence_ref" => move_proof,
      "evidence_sha256" => Digest::SHA256.file(move_proof).hexdigest
    }
    move_ledger = File.join(temporary, "relocation-receipts.jsonl")
    validate_move = lambda do |entries|
      File.write(move_ledger, entries.map { |entry| JSON.generate(entry) }.join("\n") + "\n")
      move_errors, move_coordination = [], []
      validate_derivation_receipts(move_ledger, root, {}, move_errors, move_coordination)
      [move_errors, move_coordination]
    end
    check.call(validate_move.call([move_original, relocation]) == [[], []], "hash-bound relocation preserves original derivation")
    check.call(!validate_move.call([move_original]).first.empty?, "missing source without relocation still fails")
    [
      {"source_sha256" => "0" * 64}, {"previous_source_path" => "../original.bin"},
      {"source_path" => "escape/outside.txt"}, {"source_path" => "客户及展会/missing.bin"},
      {"resolved_receipt_ids" => ["sha256:#{"5" * 64}"]}, {"resolved_receipt_ids" => [old_receipt_id, old_receipt_id]},
      {"authorized_by" => ""}, {"evidence_sha256" => "0" * 64},
      {"previous_source_path" => "客户及展会/other.bin"}, {"change_kind" => "content_change"}
    ].each do |change|
      check.call(!validate_move.call([move_original, relocation.merge(change)]).first.empty?, "unproven relocation rejected: #{change.keys.first}")
    end
    File.write(moved_source, "changed source")
    check.call(!validate_move.call([move_original, relocation]).first.empty?, "later content change cannot reuse relocation")
    File.write(moved_source, "unchanged source")
    original_path = File.join(root, "客户及展会", "original.bin")
    File.write(original_path, "unchanged source")
    check.call(!validate_move.call([move_original, relocation]).first.empty?, "a copy is not proof of relocation")
    File.unlink(original_path)
    duplicate_move = relocation.merge("receipt_id" => "sha256:#{"6" * 64}")
    check.call(!validate_move.call([move_original, relocation, duplicate_move]).first.empty?, "ambiguous relocation rejected")
    check.call(path_like_source_refs("外部调研/fixture.md") == ["外部调研/fixture.md"], "relocated research source references stay validated")
    inventory_errors = []
    inventory_coordination = []
    inventory = {
      "schema_version" => 2, "generated_at" => "2099-01-01T00:00:00+08:00",
      "vault_root" => root, "memory_root" => File.join(root, "memory"), "record_count" => 1,
      "change_counts" => { "unchanged" => 1 },
      "records" => [{
        "root" => "vault", "path" => "inside.txt", "sha256" => "0" * 64,
        "size" => 6, "modified_at" => "2099-01-01T00:00:00+08:00", "change_state" => "unchanged"
      }]
    }
    validate_source_inventory(
      "synthetic-inventory.json", inventory, root, File.join(root, "memory"),
      inventory_errors, inventory_coordination
    )
    check.call(
      inventory_errors.empty? && inventory_coordination.any? { |message| message.include?("drift") },
      "unrelated source drift warns without invalidating memory transaction"
    )
  end
  check.call(MemoryValidationContracts.snapshot_verdict([], { "data" => "before" }, { "data" => "after" }) == "deferred",
             "changed snapshot never publishes a successful validation")
  check.call(MemoryValidationContracts.snapshot_verdict(["mixed snapshot error"], { "data" => "before" }, { "data" => "after" }) == "deferred",
             "changed snapshot errors do not falsely prove corruption")
  check.call(MemoryValidationContracts.snapshot_verdict(["real error"], { "data" => "same" }, { "data" => "same" }) == "fail",
             "stable snapshot validation errors remain strict failures")
  { "verdict" => "pass", "tests_passed" => passed }
end

if ARGV == ["--self-test"]
  puts JSON.pretty_generate(validator_self_test)
  exit 0
end

# A direct validator run must see one coherent file/database generation.  When
# a semantic or index transaction already owns the lock it explicitly passes
# BUSINESS_MEMORY_INDEX_LOCK_HELD=1 to avoid self-deadlock.
validation_lock = nil
unless ENV["BUSINESS_MEMORY_INDEX_LOCK_HELD"] == "1"
  validation_lock_path = Pathname.new(File.expand_path("../state/memory-index.lock", __dir__))
  FileUtils.mkdir_p(validation_lock_path.dirname)
  validation_lock = File.open(validation_lock_path, File::RDWR | File::CREAT, 0o600)
  File.chmod(0o600, validation_lock_path)
  validation_lock.flock(File::LOCK_EX)
  ENV["BUSINESS_MEMORY_INDEX_LOCK_HELD"] = "1"
  at_exit do
    validation_lock.flock(File::LOCK_UN)
    validation_lock.close
  end
end

timing = ENV["BUSINESS_AUTOMATIC_FULL_VALIDATION"] == "1" ? ValidationProgress.new(File.expand_path("..", __dir__)) : nil
$validation_progress = timing
at_exit { timing&.finish }
begin
errors = []
coordination_points = []
bridge_root = File.expand_path("..", __dir__)
config_path = File.join(bridge_root, "config.json")
database_path = File.join(bridge_root, "state", "bridge.sqlite")

config = load_json_fail_closed(config_path, errors)
add_error(errors, "usage: validate_memory.rb [MEMORY_ROOT [EXTERNAL_ROOT_OR_LEDGER_ROOT]]") if ARGV.length > 2
configured_memory_root = config && File.expand_path(config["memory_root"].to_s)
memory_root = ARGV[0] ? File.expand_path(ARGV[0]) : configured_memory_root
validation_context_before = nil
if configured_memory_root && memory_root == configured_memory_root
  timing&.stage("before_snapshot")
  context_output, context_status = Open3.capture2(
    {"BUSINESS_VALIDATION_SNAPSHOT_PHASE" => "before_snapshot"},
    "/usr/bin/python3", File.join(bridge_root, "bin", "validation_evidence.py"), bridge_root
  )
  validation_context_before = JSON.parse(context_output)
  unless context_status.success?
    code = validation_context_before["error"]
    raise(%w[validation_permission_denied validation_inputs_changed].include?(code) ? code : "validation_context_unavailable")
  end
end
timing&.stage("files_and_links")
add_error(errors, "configured memory_root is missing") unless memory_root && File.directory?(memory_root)
vault_root = memory_root && File.dirname(memory_root)
external_argument = ARGV[1] && File.expand_path(ARGV[1])
validate_bridge_config(config_path, config, memory_root, vault_root, external_argument, errors)
if config
  configured_external_root = File.expand_path(config["external_root"].to_s)
  external_root = configured_external_root
  ledger_root = File.expand_path(config["machine_ledger_root"].to_s)
else
  external_root = external_argument || ""
  ledger_root = File.join(external_root, "machine-ledgers")
end
add_error(errors, "#{external_root}: configured external_root is missing") unless File.directory?(external_root)
add_error(errors, "#{ledger_root}: machine-ledgers directory is missing") unless File.directory?(ledger_root)

storage_counts = {}
records = []
records_by_id = {}
records_by_path = {}
external_watch_records = []
external_watch_records_by_id = {}

if memory_root && vault_root && File.directory?(memory_root)
  storage_counts = scan_storage_boundary(vault_root, errors)
  standalone_pending = Dir.glob(File.join(memory_root, "90_待确认", "*.md")).reject do |path|
    File.basename(path) == "00_待确认总表.md"
  end
  unless standalone_pending.empty?
    add_error(errors, "#{memory_root}/90_待确认: contains #{standalone_pending.length} standalone business notes; unresolved content must be annotated on canonical pages")
  end
  legacy_watch_notes = Dir.glob(File.join(memory_root, "90_待确认", "Win11文件夹监控-*.md"))
  unless legacy_watch_notes.empty?
    add_error(errors, "#{memory_root}/90_待确认: folder-watch machine notes must live in the external machine layer")
  end
  candidate_paths = CATEGORY_DIRS.flat_map { |directory| Dir.glob(File.join(memory_root, directory, "**", "*.md")) }.sort
  pending_index = File.join(memory_root, "90_待确认", "00_待确认总表.md")
  candidate_paths.reject! { |path| File.expand_path(path) == File.expand_path(pending_index) }

  candidate_paths.each do |path|
    text = read_utf8(path, errors)
    next unless text
    begin
      parsed = parse_frontmatter(text)
    rescue StandardError => e
      add_error(errors, "#{path}: YAML #{e.class}: #{e.message}")
      next
    end
    if parsed.nil?
      add_error(errors, "#{path}: malformed frontmatter around memory_id") if text.match?(/^memory_id:/)
      next
    end
    data, body, raw_frontmatter = parsed
    next unless data.key?("memory_id")

    relative = Pathname.new(path).relative_path_from(Pathname.new(memory_root)).to_s
    record = { "path" => path, "relative" => relative, "data" => data, "body" => body, "text" => text }
    records << record
    records_by_path[relative] = record
    duplicate_keys = raw_frontmatter.lines.map do |line|
      match = line.match(/\A([A-Za-z_][A-Za-z0-9_-]*):/)
      match && match[1]
    end.compact.group_by(&:itself).select { |_key, copies| copies.length > 1 }.keys
    add_error(errors, "#{path}: duplicate top-level YAML keys #{duplicate_keys.join(",")}") unless duplicate_keys.empty?

    missing = REQUIRED_FIELDS.reject { |field| data.key?(field) }
    add_error(errors, "#{path}: missing #{missing.join(",")}") unless missing.empty?
    memory_id = data["memory_id"].to_s
    if records_by_id.key?(memory_id)
      add_error(errors, "#{path}: duplicate memory_id #{memory_id} also in #{records_by_id[memory_id].fetch("path")}")
    else
      records_by_id[memory_id] = record
    end
    add_error(errors, "#{path}: invalid memory_id #{memory_id.inspect}") unless memory_id.match?(MEMORY_ID_RE)
    ALLOWED.each do |field, values|
      add_error(errors, "#{path}: invalid #{field} #{data[field].inspect}") unless values.include?(data[field])
    end
    add_error(errors, "#{path}: empty source_ref") if data["source_ref"].to_s.strip.empty?
    add_error(errors, "#{path}: applies_to must equal project:business-energy") unless data["applies_to"] == ["project:business-energy"]
    add_error(errors, "#{path}: supersedes must be an array") unless data["supersedes"].is_a?(Array)
    hash_match = data["content_hash"].to_s.match(CONTENT_SHA_RE)
    add_error(errors, "#{path}: invalid content_hash") unless hash_match
    current_hash = Digest::SHA256.hexdigest(canonical_memory_body(body))
    add_error(errors, "#{path}: content_hash does not match normalized Markdown body") if hash_match && hash_match[1] != current_hash
    %w[observed_at processed_at reviewed_at valid_until].each do |field|
      add_error(errors, "#{path}: invalid #{field}") if data.key?(field) && !time_value?(data[field])
    end
    review_metadata_issues(data).each { |issue| add_error(errors, "#{path}: #{issue}") }
    if data.key?("series_id") && !data["series_id"].nil? && !data["series_id"].to_s.match?(SERIES_ID_RE)
      add_error(errors, "#{path}: invalid series_id #{data["series_id"].inspect}")
    end
    path_like_source_refs(data["source_ref"], vault_root).each do |source_path_ref|
      resolved_source = resolve_source_ref_path(vault_root, source_path_ref)
      next if resolved_source
      # A source on the Win11 share cannot be checked while the share is not
      # mounted (SMB over ZeroTier drops for a minute at a time); that is not
      # the note being wrong, and it rolled back unrelated writes (2020-01-02).
      if source_path_ref.to_s.start_with?(WIN11_SHARE_ROOT + "/") && !win11_share_mounted?
        coordination_points << "Win11 share offline; source_ref not checked this run: #{relative}."
        next
      end
      add_error(errors, "#{path}: source_ref path is missing or unsafe #{source_path_ref.inspect}")
    end

    top_directory = relative.split("/", 2).first
    expected_category = {
      "01_关键业务认知" => "business_cognition",
      "02_内部业务资料" => "internal_material",
      "03_外部沟通" => "external_communication",
      "04_对话来源摘要" => "conversation_summary"
    }[top_directory]
    if expected_category && data["category"] != expected_category
      add_error(errors, "#{path}: category does not match directory #{top_directory}")
    end
    conversation = data["memory_scope"] == "conversation_source"
    formal = !conversation
    if conversation
      unless data["memory_kind"] == "conversation_summary" && data["category"] == "conversation_summary"
        add_error(errors, "#{path}: conversation_source must be conversation_summary")
      end
      add_error(errors, "#{path}: conversation summary must stay in 04_对话来源摘要") unless top_directory == "04_对话来源摘要"
      add_error(errors, "#{path}: conversation fact_status must be not_applicable") unless data["fact_status"] == "not_applicable"
      expected_check = data["status"] == "active" ? "matched" : (data["status"] == "review_required" ? "unverified" : nil)
      if expected_check && data["source_check"] != expected_check
        add_error(errors, "#{path}: conversation #{data["status"]} requires source_check #{expected_check}")
      end
    elsif data["memory_kind"] == "conversation_summary" || data["category"] == "conversation_summary"
      add_error(errors, "#{path}: conversation summary metadata requires conversation_source")
    end
    if formal && data["status"] == "review_required"
      add_error(errors, "#{path}: formal review_required content must be an inline canonical-page annotation, not a standalone memory note")
      unless %w[pending conflicting].include?(data["fact_status"])
        add_error(errors, "#{path}: formal review_required requires pending or conflicting")
      end
    end
    if formal && top_directory == "90_待确认" && data["status"] != "review_required"
      add_error(errors, "#{path}: formal note in 90_待确认 must be review_required")
    end
    if formal && data["status"] == "active"
      if %w[guardrail source_index].include?(data["memory_kind"])
        add_error(errors, "#{path}: active guardrail/source_index requires not_applicable") unless data["fact_status"] == "not_applicable"
      else
        add_error(errors, "#{path}: active formal memory requires confirmed") unless data["fact_status"] == "confirmed"
      end
      add_error(errors, "#{path}: active formal memory requires source_check matched") unless data["source_check"] == "matched"
    end
    if %w[guardrail source_index].include?(data["memory_kind"]) && data["fact_status"] != "not_applicable"
      add_error(errors, "#{path}: guardrail/source_index requires not_applicable")
    end
    if data["fact_status"] == "not_applicable" && !conversation && !%w[guardrail source_index].include?(data["memory_kind"])
      add_error(errors, "#{path}: not_applicable is limited to conversation_summary, guardrail, or source_index")
    end
    add_error(errors, "#{path}: confirmed requires source_check matched") if data["fact_status"] == "confirmed" && data["source_check"] != "matched"
    add_error(errors, "#{path}: discrepant requires fact_status conflicting") if data["source_check"] == "discrepant" && data["fact_status"] != "conflicting"
    add_error(errors, "#{path}: unverified cannot be confirmed") if data["source_check"] == "unverified" && data["fact_status"] == "confirmed"
    if %w[pending conflicting].include?(data["fact_status"]) && data["status"] != "review_required"
      add_error(errors, "#{path}: pending/conflicting requires review_required")
    end
    add_error(errors, "#{path}: inference is not allowed as formal business memory") if data["evidence_level"] == "inference" && formal
    if memory_id.match?(WIN_ID_RE)
      unless data["category"] == "incoming" && data["memory_scope"] == "internal_nonpublic" &&
             data["memory_kind"] == "status" && data["status"] == "review_required" &&
             data["sensitivity"] == "internal" && data["fact_status"] == "pending" &&
             top_directory == "90_待确认"
        add_error(errors, "#{path}: WIN note must be an internal incoming status pending review in 90_待确认")
      end
      case data["source_system"]
      when WINDOWS_QUICK_CAPTURE
        unless data["origin_type"] == "owner_statement" && data["evidence_level"] == "user_reported" &&
               data["source_check"] == "matched"
          add_error(errors, "#{path}: quick-capture WIN note requires owner_statement, user_reported, and matched")
        end
        unless data["source_bundle_sha256"].to_s.match?(CONTENT_SHA_RE)
          add_error(errors, "#{path}: quick-capture WIN note requires source_bundle_sha256")
        end
      when WINDOWS_FOLDER_WATCH
        add_error(errors, "#{path}: folder-watch machine note must not remain in Obsidian")
        unless data["origin_type"] == "system_observation" && data["evidence_level"] == "unverified" &&
               data["source_check"] == "unverified"
          add_error(errors, "#{path}: folder-watch WIN note requires system_observation and unverified evidence")
        end
        unless data["source_ref"].to_s.match?(/\Awindows_folder_watch:[A-Za-z0-9._-]{1,128}:[0-9a-f-]{36}\z/) &&
               data["source_ref"].to_s.split(":").last.match?(UUID_RE)
          add_error(errors, "#{path}: folder-watch source_ref must be windows_folder_watch:<source_id>:<source_entry_id>")
        end
        add_error(errors, "#{path}: folder-watch source_event_id must be a UUID") unless data["source_event_id"].to_s.match?(UUID_RE)
        add_error(errors, "#{path}: folder-watch source_entry_id must be a UUID") unless data["source_entry_id"].to_s.match?(UUID_RE)
        unless WATCH_OPERATIONS.include?(data["source_operation"].to_s)
          add_error(errors, "#{path}: invalid folder-watch source_operation #{data["source_operation"].inspect}")
        end
        if data.key?("source_bundle_sha256") && !data["source_bundle_sha256"].to_s.match?(CONTENT_SHA_RE)
          add_error(errors, "#{path}: invalid folder-watch source_bundle_sha256")
        end
        if data.key?("semantic_status")
          semantic_status = data["semantic_status"].to_s
          unless SEMANTIC_STATUSES.include?(semantic_status)
            add_error(errors, "#{path}: invalid semantic_status #{data["semantic_status"].inspect}")
          end
          if SEMANTIC_PROCESSED.include?(semantic_status)
            unless data["semantic_run_id"].to_s.match?(/\A[A-Za-z0-9._:-]{1,128}\z/)
              add_error(errors, "#{path}: processed semantic source requires semantic_run_id")
            end
            begin
              Time.iso8601(data["semantic_processed_at"].to_s)
            rescue ArgumentError
              add_error(errors, "#{path}: processed semantic source requires ISO semantic_processed_at")
            end
            unless data["semantic_targets"].is_a?(Array) && data["semantic_targets"].all? { |target| target.to_s.match?(MEMORY_ID_RE) }
              add_error(errors, "#{path}: processed semantic source requires semantic_targets array")
            end
          end
        end
      else
        add_error(errors, "#{path}: WIN note source_system must be #{WINDOWS_QUICK_CAPTURE} or #{WINDOWS_FOLDER_WATCH}")
      end
    elsif data["category"] == "incoming"
      add_error(errors, "#{path}: incoming category is reserved for WIN notes")
    end

    basename = File.basename(path, ".md")
    add_error(errors, "#{path}: duplicate H1") if text.lines.any? { |line| line.strip == "# #{basename}" }
    add_error(errors, "#{path}: possible API key") if text.match?(/\bsk-[A-Za-z0-9_-]{20,}\b|\bAKIA[0-9A-Z]{16}\b/)
    # 2020-01-02 Example Owner 明确决定：本记忆库是本人自用的内部库，不做联系方式脱敏。
    # 联系人邮箱与电话属于可复用的业务事实，禁止它们反而降低记忆库可用性。
    # 因此移除邮箱与手机号的强制检查；凭据类（API key）检查保留。
    # 手机号检查同上，于 2020-01-02 一并移除。
  rescue StandardError => e
    add_error(errors, "#{path}: note validation error #{e.class}: #{e.message}")
  end

  business_claims = load_business_claims(bridge_root, config_path, errors)
  business_claims.each do |claim|
    claim_id = claim["claim_id"].to_s
    if records_by_id.key?(claim_id)
      add_error(errors, "#{claim['relative_path']}: claim id #{claim_id} duplicates standalone memory #{records_by_id[claim_id].fetch('path')}")
    else
      records_by_id[claim_id] = { "path" => File.join(vault_root, claim["relative_path"].to_s), "claim" => claim }
    end
  end
  validate_business_claims(bridge_root, config_path, errors)

  records.each do |record|
    supersedes = record.fetch("data")["supersedes"]
    next unless supersedes.is_a?(Array)
    add_error(errors, "#{record.fetch("path")}: duplicate supersedes IDs") if supersedes.uniq.length != supersedes.length
    supersedes.each do |memory_id|
      add_error(errors, "#{record.fetch("path")}: unresolved supersedes #{memory_id}") unless records_by_id.key?(memory_id.to_s)
      add_error(errors, "#{record.fetch("path")}: cannot supersede itself") if memory_id.to_s == record.fetch("data")["memory_id"].to_s
    end
    data = record.fetch("data")
    if data["memory_id"].to_s.match?(WIN_ID_RE) && SEMANTIC_PROCESSED.include?(data["semantic_status"])
      Array(data["semantic_targets"]).each do |target|
        add_error(errors, "#{record.fetch("path")}: unresolved semantic target #{target}") unless records_by_id.key?(target.to_s)
      end
    end
  end
  series_contract_issues(records).each { |issue| add_error(errors, issue) }
  supersession_graph = records.to_h do |record|
    [record.fetch("data")["memory_id"].to_s, Array(record.fetch("data")["supersedes"]).map(&:to_s)]
  end
  supersession_cycles(supersession_graph).each do |cycle|
    add_error(errors, "supersedes cycle detected: #{cycle.join(" -> ")}")
  end
  marine_current = records.find do |record|
    data = record.fetch("data")
    data["series_id"] == "marine-target-list" && data["status"] == "active" && data["fact_status"] == "confirmed"
  end
  if marine_current
    source_ref = marine_current.fetch("data")["source_ref"].to_s
    source_path = real_contained_path(vault_root, source_ref)
    expected_hash = "40c51fe55b2d296469748887b748c3df214e36f559007b85d67e7b76a11c5caf"
    unless source_path && File.file?(source_path) && Digest::SHA256.file(source_path).hexdigest == expected_hash
      add_error(errors, "#{marine_current.fetch("path")}: V11.1 current workbook hash mismatch")
    end
  end
  external_watch_root = File.join(external_root, WATCH_NOTES_RELATIVE)
  external_watch_records, external_watch_records_by_id = validate_external_watch_notes(
    external_watch_root, records_by_id, errors
  )
  external_watch_records.each do |record|
    data = record.fetch("data")
    next unless SEMANTIC_PROCESSED.include?(data["semantic_status"])

    Array(data["semantic_targets"]).each do |target|
      add_error(errors, "#{record.fetch('path')}: unresolved semantic target #{target}") unless records_by_id.key?(target.to_s)
    end
  end
  curated_ids = records_by_id.keys.reject { |memory_id| memory_id.match?(WIN_ID_RE) }
  missing_curated = REQUIRED_BASELINE_CURATED_IDS - curated_ids
  add_error(errors, "missing expected curated memory IDs: #{missing_curated.join(",")}") unless missing_curated.empty?

  vault_markdown = Dir.glob(File.join(vault_root, "**", "*.md")).select { |path| File.file?(path) && !ignored_system_or_office_lock?(path) }
  memory_markdown = vault_markdown.select { |path| path.start_with?("#{memory_root}#{File::SEPARATOR}") }
  memory_index = memory_markdown.to_h do |path|
    [Pathname.new(path).relative_path_from(Pathname.new(memory_root)).to_s.sub(/\.md\z/, ""), path]
  end
  vault_index = vault_markdown.to_h do |path|
    [Pathname.new(path).relative_path_from(Pathname.new(vault_root)).to_s.sub(/\.md\z/, ""), path]
  end
  basename_index = vault_markdown.group_by { |path| File.basename(path, ".md") }
  validation_paths = INDEX_RELATIVES.map { |relative| File.join(memory_root, relative) } + records.map { |record| record.fetch("path") }
  validation_paths.uniq.each do |path|
    if File.file?(path)
      text = read_utf8(path, errors)
      validate_links(path, text, vault_root, memory_index, vault_index, basename_index, errors) if text
    else
      add_error(errors, "#{path}: required memory index is missing")
    end
  end

  vault_markdown.each do |path|
    text = read_utf8(path, errors)
    next unless text
    add_error(errors, "#{path}: stale reference to 00_内部业务记忆总表") if text.include?("00_内部业务记忆总表")
    next unless path.start_with?("#{memory_root}#{File::SEPARATOR}")
    relative = Pathname.new(path).relative_path_from(Pathname.new(memory_root)).to_s
    next if relative && (relative == "91_归档" || relative.start_with?("91_归档/"))

    basename = File.basename(path, ".md")
    add_error(errors, "#{path}: duplicate H1") if text.lines.any? { |line| line.strip == "# #{basename}" }
  end
  old_index = File.join(memory_root, "00_内部业务记忆总表.md")
  add_error(errors, "#{old_index}: obsolete index must be absent") if File.exist?(old_index)

  overview_path = File.join(memory_root, "00_记忆总览.md")
  time_metadata_paths = [
    overview_path,
    File.join(memory_root, "99_规则与模板", "系统状态.md")
  ]
  time_metadata_paths.each do |time_path|
    next unless File.file?(time_path) && (text = read_utf8(time_path, errors))
    parsed = parse_frontmatter(text)
    if parsed
      overview_time_metadata_issues(parsed[0]).each do |issue|
        add_error(errors, "#{time_path}: #{issue}")
      end
    else
      add_error(errors, "#{time_path}: missing or invalid front matter")
    end
  end
  ai_index_path = File.join(memory_root, "99_规则与模板", "AI检索总表.md")
  pending_index_path = File.join(memory_root, "90_待确认", "00_待确认总表.md")
  if File.file?(ai_index_path) && (text = read_utf8(ai_index_path, errors))
    ai_rows = parse_index_rows(ai_index_path, text, errors)
    expected_ai = records.select do |record|
      data = record.fetch("data")
      data["status"] == "active" && data["memory_scope"] != "conversation_source" &&
        %w[confirmed not_applicable].include?(data["fact_status"]) &&
        %w[guardrail decision source_index status open_question fact judgment].include?(data["memory_kind"])
    end
    validate_memory_index(ai_index_path, text, ai_rows, expected_ai, records_by_path, memory_root, errors, allow_omitted: true)
    add_error(errors, "#{ai_index_path}: customer workbook authority guardrail missing") unless text.include?("客户与联系人明细继续以小满 CRM")
  end
  if File.file?(pending_index_path) && (text = read_utf8(pending_index_path, errors))
    parsed = parse_frontmatter(text)
    if parsed.nil? || parsed[0]["document_type"] != "business_unresolved_claim_index"
      add_error(errors, "#{pending_index_path}: must be the generated inline-claim navigation index")
    elsif parsed[0]["total_records"].to_i != business_claims.length
      add_error(errors, "#{pending_index_path}: total_records does not match inline claims")
    end
    add_error(errors, "#{pending_index_path}: standalone-review wording remains") if text.include?("全部原记录仍在 `90_待确认/`")
  else
    add_error(errors, "#{pending_index_path}: generated inline-claim navigation index is missing")
  end
  if config && config["index_budgets"].is_a?(Hash)
    budgets = config["index_budgets"]
    chars_per_token = budgets["chars_per_token"].to_f
    token_counts = {}
    {
      "overview" => File.join(memory_root, "00_记忆总览.md"),
      "ai" => ai_index_path,
      "pending" => pending_index_path
    }.each do |name, path|
      next unless File.file?(path)
      token_counts[name] = (File.read(path, encoding: "UTF-8").length / chars_per_token).ceil
      hard = budgets["#{name}_hard_tokens"].to_i
      add_error(errors, "#{path}: estimated tokens #{token_counts[name]} exceed hard limit #{hard}") if token_counts[name] > hard
    end
    combined = token_counts.values.sum
    combined_hard = budgets["combined_hard_tokens"].to_i
    add_error(errors, "memory entry indexes estimated tokens #{combined} exceed combined hard limit #{combined_hard}") if combined > combined_hard
  end
end

conversation_ledger_path = File.join(ledger_root, "对话同步台账.json")
internal_ledger_path = config ? config.dig("source_governance", "legacy_ledger").to_s : File.join(ledger_root, "内部记忆整理台账.json")
source_inventory_path = config ? config.dig("source_governance", "inventory").to_s : File.join(ledger_root, "内部来源资产清单.json")
derivation_receipts_path = config ? config.dig("source_governance", "derivation_receipts").to_s : File.join(ledger_root, "记忆派生回执.jsonl")
conversation_ledger = load_json_fail_closed(conversation_ledger_path, errors)
internal_ledger = load_json_fail_closed(internal_ledger_path, errors)
source_inventory = load_json_fail_closed(source_inventory_path, errors)
conversation_notes = records.select { |record| record.fetch("data")["memory_scope"] == "conversation_source" }
chatgpt_conversation_notes = conversation_notes.select do |record|
  record.fetch("data")["source_system"] == "chatgpt"
end
conversation_record_count = validate_conversation_ledger(
  conversation_ledger_path, conversation_ledger, chatgpt_conversation_notes, memory_root.to_s, errors
)
internal_record_count, source_outcome_counts = validate_internal_ledger(
  internal_ledger_path, internal_ledger, vault_root.to_s, memory_root.to_s, errors
)
source_inventory_count, source_change_counts = validate_source_inventory(
  source_inventory_path, source_inventory, vault_root.to_s, memory_root.to_s, errors, coordination_points
)
derivation_receipt_count, derivation_receipt_counts = validate_derivation_receipts(
  derivation_receipts_path, vault_root.to_s, records_by_id, errors, coordination_points
)
win_records = records.select { |record| record.fetch("data")["memory_id"].to_s.match?(WIN_ID_RE) }
quick_capture_records = win_records.select do |record|
  record.fetch("data")["source_system"] == WINDOWS_QUICK_CAPTURE
end
timing&.stage("bridge_database", {memory_notes: records.length, source_assets: source_inventory_count})
database_counts = validate_bridge_database(
  database_path, memory_root.to_s, external_root, win_records + external_watch_records, errors, coordination_points
)
if config && File.file?(File.join(bridge_root, "bin", "email_phases.py"))
  phase_db = File.join(config.dig("email_sync", "state_root").to_s, "state", "email-sync.sqlite")
  if File.file?(phase_db)
    phase_out, phase_status = Open3.capture2("/usr/bin/python3", File.join(bridge_root, "bin", "email_phases.py"), "--config", config_path, "verify")
    phase_result = JSON.parse(phase_out) rescue {}
    add_error(errors, "email supplements: #{phase_result['error_code'] || 'verification_failed'}") unless phase_status.success? && phase_result["status"] == "ok"
  end
end
timing&.stage("semantic_database")
semantic_database_counts = if ENV["BUSINESS_SEMANTIC_WRITE_IN_PROGRESS"] == "1"
                             { "validation" => "deferred_inside_semantic_transaction" }
                           elsif config
                             validate_semantic_database(
                               config["semantic_database"].to_s, config["semantic_runs_root"].to_s, errors
                             )
                           else
                             {}
                           end

status_path = memory_root && File.join(memory_root, "99_规则与模板", "系统状态.md")
overview_path = memory_root && File.join(memory_root, "00_记忆总览.md")
if status_path && overview_path && File.file?(status_path) && File.file?(overview_path)
  status_text = read_utf8(status_path, errors)
  overview_text = read_utf8(overview_path, errors)
  begin
    status_parsed = status_text && parse_frontmatter(status_text)
    overview_parsed = overview_text && parse_frontmatter(overview_text)
    if status_parsed && overview_parsed
      external_pending = external_watch_records.count do |record|
        !SEMANTIC_PROCESSED.include?(record.fetch("data")["semantic_status"])
      end
      formal_current = records.count do |record|
        data = record.fetch("data")
        data["status"] == "active" && data["memory_scope"] != "conversation_source" &&
          %w[confirmed not_applicable].include?(data["fact_status"])
      end
      expected_status = {
        "memory_notes" => records.length,
        "formal_memory_notes" => records.count { |record| record.fetch("data")["memory_scope"] != "conversation_source" },
        "formal_current_notes" => formal_current,
        "conversation_pending_notes" => records.count do |record|
          data = record.fetch("data")
          data["memory_scope"] == "conversation_source" && data["status"] == "review_required"
        end,
        "quick_capture_notes" => quick_capture_records.length,
        "inline_pending_claims" => business_claims.length,
        "external_watch_notes" => external_watch_records.length,
        "external_watch_pending_notes" => external_pending,
        "index_dirty_generation" => database_counts["index_dirty_generation"],
        "index_applied_generation" => database_counts["index_applied_generation"],
        "index_last_refreshed_at" => database_counts["index_last_refreshed_at"],
        "index_last_error" => database_counts["index_last_error"],
        "index_state" => database_counts["index_state"]
      }
      status_snapshot_issues(status_parsed[0], overview_parsed[0], expected_status).each do |issue|
        add_error(errors, "#{status_path}: #{issue}")
      end
      add_error(errors, "#{status_path}: generated status notice missing") unless status_text.include?("索引刷新链自动生成")
      add_error(errors, "#{status_path}: snapshot health boundary missing") unless status_text.include?("快照，不等于实时服务健康")
    else
      add_error(errors, "#{status_path}: generated status or overview front matter is invalid")
    end
  rescue StandardError => e
    add_error(errors, "#{status_path}: generated status validation error #{e.class}: #{e.message}")
  end
end

timing&.stage("result_assembly")
errors = errors.compact.map(&:to_s).uniq
metadata = records.map { |record| record.fetch("data") }
external_watch_pending_count = external_watch_records.count do |record|
  !SEMANTIC_PROCESSED.include?(record.fetch("data")["semantic_status"])
end
external_watch_semantic_counts = count_values(
  external_watch_records.map { |record| record.fetch("data")["semantic_status"] || "pending" }
)
result = {
  "verdict" => errors.empty? ? "pass" : "fail",
  "memory_notes" => records.length,
  "curated_memory_notes" => metadata.count { |data| !data["memory_id"].to_s.match?(WIN_ID_RE) },
  "formal_memory_notes" => metadata.count { |data| data["memory_scope"] != "conversation_source" },
  "formal_pending_notes" => metadata.count { |data| data["memory_scope"] != "conversation_source" && data["status"] == "review_required" && data["source_system"] != WINDOWS_QUICK_CAPTURE },
  "inline_pending_claims" => defined?(business_claims) && business_claims.is_a?(Array) ? business_claims.length : 0,
  "conversation_pending_notes" => metadata.count { |data| data["memory_scope"] == "conversation_source" && data["status"] == "review_required" },
  "quick_capture_notes" => quick_capture_records.length,
  "external_watch_notes" => external_watch_records.length,
  "external_watch_pending_notes" => external_watch_pending_count,
  "external_watch_semantic_status_counts" => external_watch_semantic_counts,
  "win_memory_notes" => quick_capture_records.length + external_watch_records.length,
  "formal_business_notes" => metadata.count { |data| data["memory_scope"] != "conversation_source" },
  "conversation_source_notes" => conversation_notes.length,
  "unique_memory_ids" => records_by_id.length + external_watch_records_by_id.length,
  "status_counts" => count_values(metadata.map { |data| data["status"] }),
  "source_check_counts" => count_values(metadata.map { |data| data["source_check"] }),
  "fact_status_counts" => count_values(metadata.map { |data| data["fact_status"] }),
  "kind_counts" => count_values(metadata.map { |data| data["memory_kind"] }),
  "conversation_records" => conversation_record_count,
  "legacy_internal_source_records" => internal_record_count,
  "legacy_source_outcome_counts" => source_outcome_counts,
  "current_source_assets" => source_inventory_count,
  "source_change_counts" => source_change_counts,
  "derivation_receipts" => derivation_receipt_count,
  "derivation_receipt_counts" => derivation_receipt_counts,
  "storage_boundary_counts" => storage_counts,
  "bridge_database_counts" => database_counts,
  "semantic_database_counts" => semantic_database_counts,
  "machine_ledger_root" => ledger_root,
  "coordination_points" => coordination_points.uniq,
  "errors" => errors
}
if configured_memory_root && memory_root == configured_memory_root && ENV["BUSINESS_SEMANTIC_WRITE_IN_PROGRESS"] != "1"
  timing&.stage("after_snapshot")
  context_after_output, context_after_status = Open3.capture2(
    {"BUSINESS_VALIDATION_SNAPSHOT_PHASE" => "after_snapshot"},
    "/usr/bin/python3", File.join(bridge_root, "bin", "validation_evidence.py"), bridge_root
  )
  context_after = JSON.parse(context_after_output)
  unless context_after_status.success?
    code = context_after["error"]
    raise(%w[validation_permission_denied validation_inputs_changed].include?(code) ? code : "validation_context_unavailable")
  end
  if MemoryValidationContracts.snapshot_verdict(errors, validation_context_before, context_after) == "deferred"
    # An external edit can bypass our writer lock.  A mixed snapshot is neither
    # a successful validation nor proof that the business data is damaged.
    result["verdict"] = "deferred"
    result["error_code"] = "validation_inputs_changed"
    result["errors"] = []
    puts JSON.pretty_generate(result)
    exit 2
  end
  timing&.stage("receipt_publication")
  if errors.empty?
    bridge_counts = result["bridge_database_counts"].is_a?(Hash) ? result["bridge_database_counts"] : {}
    atomic_write_json(
      File.join(bridge_root, "state", "last-memory-validation-success.json"),
      {
        "schema_version" => 2,
        "full_validation" => true,
        "run_id" => ENV["BUSINESS_VALIDATION_RUN_ID"],
        "recorded_at" => Time.now.getlocal.iso8601,
        "verdict" => "pass",
        "memory_notes" => result["memory_notes"],
        "current_source_assets" => result["current_source_assets"],
        "index_dirty_generation" => bridge_counts["index_dirty_generation"],
        "index_applied_generation" => bridge_counts["index_applied_generation"],
        "index_state" => bridge_counts["index_state"],
        "validator_sha256" => Digest::SHA256.file(__FILE__).hexdigest,
        "validation_context" => context_after
      }
    )
  end
end
puts JSON.pretty_generate(result)
exit(errors.empty? ? 0 : 1)
rescue StandardError => e
  if e.message == "validation_inputs_changed"
    puts JSON.pretty_generate({"verdict" => "deferred", "error_code" => "validation_inputs_changed", "errors" => []})
    exit 2
  end
  fallback_errors = defined?(errors) && errors.is_a?(Array) ? errors : []
  fallback_errors << "validator internal error #{e.class}: #{e.message}"
  puts JSON.pretty_generate({
    "verdict" => "fail",
    "error_code" => (%w[validation_permission_denied validation_context_unavailable].include?(e.message) ? e.message : "validation_internal_error"),
    "memory_notes" => defined?(records) && records.is_a?(Array) ? records.length : 0,
    "errors" => fallback_errors.compact.map(&:to_s).uniq
  })
  exit 1
end
