# frozen_string_literal: true
require_relative "archive_guard"
ArchiveGuard.require_opt_in!

require 'json'
require 'time'
require 'digest'
require 'fileutils'

# Content-free progress only; stdout remains the validator result protocol.
class ValidationProgress
  def initialize(root)
    @state = File.join(root, 'state')
    @run_id = ENV['BUSINESS_VALIDATION_RUN_ID']
    @code = Digest::SHA256.file(__FILE__).hexdigest
    @started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    @phase_started = @started
    @phase = 'startup'
    @counts = {}
    @mutex = Mutex.new
    @dropped = 0
    emit('started')
    @thread = Thread.new { loop { sleep 5; emit('progress') } }
  end

  def stage(name, counts = {})
    emit('stage_finished')
    @mutex.synchronize do
      @phase = name
      @phase_started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      @counts.merge!(counts)
    end
    emit('stage_started')
  end

  def count(name, amount = 1)
    @mutex.synchronize { @counts[name] = (@counts[name] || 0) + amount }
  end

  def emit(event)
    @mutex.synchronize do
      now = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      row = { schema_version: 1, recorded_at: Time.now.getlocal.iso8601,
              run_id: @run_id, component: 'ruby_validator', pid: Process.pid, ppid: Process.ppid,
              code_sha256: @code, event: event, phase: @phase,
              phase_seconds: (now - @phase_started).round(6), elapsed_seconds: (now - @started).round(6),
              counts: @counts, diagnostic_dropped: @dropped }
      @lock ||= File.open(File.join(@state, 'validation-progress.lock'), File::WRONLY | File::CREAT | File::NOFOLLOW, 0o600)
      raise IOError, 'busy' unless @lock.flock(File::LOCK_EX | File::LOCK_NB)
      begin
        path = File.join(@state, 'validation-progress.jsonl')
        info = File.exist?(path) ? File.stat(path) : nil
        if info && info.size >= 4 * 1024 * 1024
          2.downto(1) { |i| File.rename("#{path}.#{i}", "#{path}.#{i+1}") if File.exist?("#{path}.#{i}") }
          File.rename(path, "#{path}.1")
          info = nil
        end
        if @journal && (!info || [info.dev, info.ino] != [@journal.stat.dev, @journal.stat.ino])
          @journal.close
          @journal = nil
        end
        @journal ||= File.open(path, File::WRONLY | File::CREAT | File::APPEND | File::NOFOLLOW, 0o600)
        @journal.syswrite(JSON.generate(row) + "\n")
      ensure
        @lock.flock(File::LOCK_UN)
      end
    end
  rescue StandardError
    @dropped += 1
    warn 'validation_progress_unavailable' if @dropped == 1
  end

  def finish
    @thread.kill
    @thread.join
    emit('stage_finished')
    emit('finished')
    @journal&.close
    @lock&.close
  rescue StandardError
    warn 'validation_progress_unavailable'
  end
end
