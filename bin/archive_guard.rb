# frozen_string_literal: true
module ArchiveGuard
  def self.require_opt_in!
    root = ENV['SYNC_CENTER_HOME']
    allowed = ENV['SYNC_CENTER_ENABLE_ARCHIVED_CODE'] == '1' && root &&
              root.start_with?('/') && File.directory?(root)
    abort 'Archived integration is disabled; set explicit opt-in and an existing absolute isolated directory.' unless allowed
  end
end
