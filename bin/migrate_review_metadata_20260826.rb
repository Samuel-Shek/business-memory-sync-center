#!/usr/bin/env ruby
# frozen_string_literal: true

require_relative "archive_guard"
ArchiveGuard.require_opt_in!

require "date"
require "json"
require "pathname"
require "securerandom"
require "yaml"

ROOT = Pathname.new(File.expand_path("..", __dir__))
CONFIG = JSON.parse(ROOT.join("config.json").read(encoding: "UTF-8"))
MEMORY_ROOT = Pathname.new(CONFIG.fetch("memory_root"))
WATCH_NOTES_ROOT = Pathname.new(CONFIG.fetch("watch_notes_root"))
CATEGORY_DIRS = %w[01_关键业务认知 02_内部业务资料 03_外部沟通 04_对话来源摘要 90_待确认].freeze

def parse_frontmatter(text)
  match = text.match(/\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\z)(.*)\z/m)
  return nil unless match

  data = YAML.safe_load(match[1], permitted_classes: [Date, Time], aliases: false) || {}
  [data, match[1], match[2]]
end

def set_scalar(raw, key, value)
  pattern = /^#{Regexp.escape(key)}:.*$/
  return raw.sub(pattern, "#{key}: #{value}") if raw.match?(pattern)

  lines = raw.lines
  insertion = lines.index { |line| line.start_with?("reviewed_at:") } || lines.length
  lines.insert(insertion, "#{key}: #{value}\n")
  lines.join
end

def atomic_write(path, content)
  mode = path.stat.mode & 0o777
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

apply = ARGV.delete("--apply")
raise "usage: migrate_review_metadata_20260826.rb [--apply]" unless ARGV.empty?

changes = []
paths = CATEGORY_DIRS.flat_map { |directory| Dir.glob(MEMORY_ROOT.join(directory, "**", "*.md")) }
paths.concat(Dir.glob(WATCH_NOTES_ROOT.join("Win11文件夹监控-*.md")))
paths.sort.each do |name|
    path = Pathname.new(name)
    parsed = parse_frontmatter(path.read(encoding: "UTF-8"))
    next unless parsed
    data, raw, body = parsed
    next unless data["memory_id"]
    next if data["reviewed_at"].nil? || data.key?("reviewed_by") || data.key?("review_mode")

    processed_at = data["processed_at"] || data["reviewed_at"]
    updated = set_scalar(raw, "processed_at", JSON.generate(processed_at.to_s))
    updated = set_scalar(updated, "reviewed_at", "null")
    changes << path
    atomic_write(path, "---\n#{updated.rstrip}\n---\n#{body.sub(/\A\n/, "")}") if apply
end

puts JSON.pretty_generate(
  "verdict" => "pass",
  "mode" => apply ? "applied" : "dry_run",
  "changed" => changes.length,
  "paths" => changes.map(&:to_s)
)
