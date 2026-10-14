# Metadata-only maintenance. Caller holds the shared memory index lock.
require_relative "archive_guard"
ArchiveGuard.require_opt_in!

require "json"
require "digest"
require "fileutils"
require "time"
require "securerandom"

module SourceInventoryMaintenance
  module_function

  def atomic(path, text)
    raise "inventory_symlink" if File.symlink?(path)
    FileUtils.mkdir_p(File.dirname(path), mode: 0o700)
    temporary = "#{path}.tmp-#{Process.pid}-#{SecureRandom.hex(4)}"
    File.open(temporary, File::WRONLY | File::CREAT | File::EXCL, 0o600) do |file|
      file.write(text)
      file.flush
      file.fsync
    end
    File.rename(temporary, path)
  ensure
    File.unlink(temporary) if temporary && File.exist?(temporary)
  end

  def encoded(value)
    JSON.pretty_generate(value) + "\n"
  end

  def signature(records)
    records.map { |row| row.reject { |key, _| key == "change_state" } }
      .sort_by { |row| [row.fetch("root"), row.fetch("path")] }
  end

  def absolute(vault, row)
    path = row.fetch("path")
    return File.expand_path(path, vault) if row["root"] == "vault" && !path.start_with?("/") && !path.split("/").include?("..")
    return path if row["root"] == "local" && path.start_with?("/")
    raise "inventory_path_invalid"
  end

  def resume(path)
    return unless File.file?(path)
    transaction = JSON.parse(File.read(path))
    return if transaction["state"] == "complete"
    raise "inventory_transaction_invalid" unless transaction["state"] == "prepared"
    # Check every target before resuming any write; unrelated user changes win.
    transaction.fetch("writes").each do |write|
      target = write.fetch("path")
      raise "inventory_symlink" if File.symlink?(target)
      current = File.file?(target) ? Digest::SHA256.file(target).hexdigest : nil
      raise "inventory_concurrent_change" unless [write["before_sha256"], write.fetch("after_sha256")].include?(current)
    end
    transaction.fetch("writes").each do |write|
      next if File.file?(write["path"]) && Digest::SHA256.file(write["path"]).hexdigest == write["after_sha256"]
      atomic(write["path"], write.fetch("after"))
    end
    transaction["state"] = "complete"
    transaction["completed_at"] = Time.now.getlocal("+08:00").iso8601
    atomic(path, encoded(transaction))
    transaction.fetch("writes").count { |write| write["path"].end_with?(".md") }
  end

  def apply(vault, memory, path, previous, inventory)
    root = File.dirname(path)
    journal = File.join(root, "source-inventory-transaction.json")
    resumed_refs = resume(journal).to_i
    previous = JSON.parse(File.read(path)) if File.file?(path)
    old = Array(previous["records"])
    current = inventory.fetch("records")
    raise "inventory_scan_incomplete" if current.any? { |row| row["change_state"] == "hash_unavailable" }
    if signature(old) == signature(current) && previous["pruned_roots"] == inventory["pruned_roots"]
      return {"status" => "no_change", "records" => old.length, "references_updated" => resumed_refs,
              "generated_at" => previous["generated_at"]}
    end
    indexed = current.to_h { |row| [[row["root"], row["path"]], row] }
    by_hash = current.group_by { |row| row["sha256"] }
    moves, missing = [], []
    old.each do |row|
      next if indexed.key?([row["root"], row["path"]])
      candidates = by_hash.fetch(row["sha256"], [])
      if row["sha256"].to_s.match?(/\A[0-9a-f]{64}\z/) && candidates.length == 1
        target = candidates.first
        from, to = absolute(vault, row), absolute(vault, target)
        if !File.exist?(from) && File.file?(to) && !File.symlink?(to) && Digest::SHA256.file(to).hexdigest == row["sha256"]
          moves << {"from" => from, "to" => to, "sha256" => row["sha256"]}
          next
        end
      end
      missing << row.merge("reason" => candidates.length > 1 ? "ambiguous_relocation" : "source_missing")
    end
    writes = []
    Dir.glob(File.join(memory, "**", "*.md")).sort.each do |note|
      next if File.symlink?(note)
      before = File.read(note, encoding: "UTF-8")
      next unless before.start_with?("---\n")
      closing = before.index("\n---", 4)
      next unless closing
      header = before[0...closing]
      refs = header.lines.select { |line| line.start_with?("source_ref:") }
      next unless refs.length == 1
      original = refs.first
      literal = original.sub(/\Asource_ref:\s*/, "").strip
      begin
        value = literal.start_with?('"') ? JSON.parse(literal) : literal
      rescue JSON::ParserError
        next
      end
      next unless value.is_a?(String)
      updated = value.dup
      moves.each do |move|
        # Only whole source references (plus an optional anchor) are automatic.
        if updated == move["from"] || updated.start_with?(move["from"] + "#")
          updated = move["to"] + updated[move["from"].length..-1].to_s
        end
      end
      next if updated == value
      replacement = "source_ref: #{JSON.generate(updated)}\n"
      after = header.sub(original, replacement) + before[closing..-1]
      writes << {"path" => note, "before_sha256" => Digest::SHA256.hexdigest(before),
                 "after_sha256" => Digest::SHA256.hexdigest(after), "before" => before, "after" => after}
    end
    status_path = File.join(root, "source-inventory-status.json")
    prior_status = File.file?(status_path) ? JSON.parse(File.read(status_path)) : {}
    outstanding = (Array(prior_status["missing"]) + missing).uniq { |row| [row["root"], row["path"], row["sha256"]] }
    outstanding.reject! { |row| indexed.key?([row["root"], row["path"]]) || moves.any? { |move| move["from"] == absolute(vault, row) } }
    status = {"schema_version" => 1, "updated_at" => inventory["generated_at"], "record_count" => current.length,
              "missing_count" => outstanding.length, "missing" => outstanding, "relocations" => moves,
              "references_updated" => writes.length, "status" => outstanding.empty? ? "current" : "attention"}
    {path => encoded(inventory), status_path => encoded(status)}.each do |target, after|
      before = File.file?(target) ? File.read(target) : nil
      writes << {"path" => target, "before_sha256" => before && Digest::SHA256.hexdigest(before),
                 "after_sha256" => Digest::SHA256.hexdigest(after), "before" => before, "after" => after}
    end
    history = File.join(root, "source-inventory-history", Time.now.strftime("%Y%m%dT%H%M%S") + "-" + SecureRandom.hex(4) + ".json")
    transaction = {"schema_version" => 1, "state" => "prepared", "created_at" => inventory["generated_at"],
                   "previous_inventory" => previous, "relocations" => moves, "missing" => missing, "writes" => writes}
    atomic(history, encoded(transaction))
    atomic(journal, encoded(transaction))
    resume(journal)
    {"status" => "refreshed", "records" => current.length, "references_updated" => status["references_updated"],
     "relocated" => moves.length, "missing" => outstanding.length, "generated_at" => inventory["generated_at"]}
  end
end
