# Pure validation predicates shared with isolated regression tests.
require_relative "archive_guard"
ArchiveGuard.require_opt_in!

require "json"
require "time"

module MemoryValidationContracts
  def self.snapshot_verdict(errors, before, after)
    return "deferred" unless before == after
    errors.empty? ? "pass" : "fail"
  end

  def self.deleted_before_capture?(row, previous, events)
    return false unless row["operation"] == "deleted" && row["status"] == "tombstoned"
    return false unless row["previous_sha256"].nil? && row["current_sha256"].nil?
    return false unless row["previous_source_relative_path"] == row["source_relative_path"]
    return false unless previous && row["previous_event_id"] == previous["event_id"]
    return false unless row["transition_sequence"].to_i == previous["transition_sequence"].to_i + 1
    seen = {}
    current = previous
    loop do
      return false unless current && !seen[current["event_id"]]
      seen[current["event_id"]] = true
      return false unless %w[source_id source_entry_id source_relative_path].all? { |key| current[key] == row[key] }
      return false unless current["previous_sha256"].nil? && current["current_sha256"].nil?
      return false unless current["status"].to_s.match?(/\Acapture_error:[a-z0-9_]{1,64}\z/)
      payload = JSON.parse(current["items_json"].to_s)
      return false unless payload["items"] == [] && current["status"] == "capture_error:#{payload["capture_error"]}"
      if %w[baseline added].include?(current["operation"])
        return current["previous_event_id"].nil? && current["previous_source_relative_path"].nil? && current["transition_sequence"].to_i == 1
      end
      return false unless current["operation"] == "modified" && current["previous_source_relative_path"] == row["source_relative_path"]
      predecessor = events[current["previous_event_id"]]
      return false unless predecessor && current["transition_sequence"].to_i == predecessor["transition_sequence"].to_i + 1
      current = predecessor
    end
  rescue JSON::ParserError, TypeError
    false
  end

  def self.valid_blocked_run?(run)
    return false unless run["status"] == "blocked" && run["kind"] == "incremental" && run["error_code"] == "batch_limit"
    return false unless run["writes_count"] == 0 && run["transaction_id"].to_s.empty?
    return false unless %w[batch_path batch_sha256].all? { |key| run[key].to_s.empty? }
    return false unless %w[content_items total_bytes].all? { |key| run[key].is_a?(Integer) && run[key] >= 0 }
    return false unless run["content_items"] > 0 && !run["error_message"].to_s.empty?
    Time.iso8601(run["created_at"].to_s)
    true
  rescue ArgumentError
    false
  end
end
