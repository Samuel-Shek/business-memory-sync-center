require_relative "archive_guard"
ArchiveGuard.require_opt_in!

module Win11BatchSelection
  module_function

  # Items already contain all events for one source entry. Never split an item.
  def select(items, max_items, max_bytes)
    selected, remaining, oversized = [], [], []
    count = bytes = 0
    items.each do |item|
      unless item["analysis_required"]
        selected << item
        next
      end
      size = item.fetch("size_bytes")
      if size > max_bytes
        oversized << item
      elsif count < max_items && bytes + size <= max_bytes
        selected << item
        count += 1
        bytes += size
      else
        remaining << item
      end
    end
    {"items" => selected, "remaining" => remaining, "oversized" => oversized,
     "content_items" => count, "total_bytes" => bytes}
  end
end
