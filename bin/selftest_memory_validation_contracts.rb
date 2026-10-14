#!/usr/bin/env ruby
require_relative "archive_guard"
ArchiveGuard.require_opt_in!

require 'minitest/autorun'
require_relative 'memory_validation_contracts'

class ValidationContractTest < Minitest::Test
  def chain
    first = { 'event_id'=>'one', 'source_id'=>'fixture', 'source_entry_id'=>'entry', 'source_relative_path'=>'never.txt',
              'previous_source_relative_path'=>nil, 'operation'=>'added', 'status'=>'capture_error:source_missing',
              'previous_sha256'=>nil, 'current_sha256'=>nil, 'previous_event_id'=>nil, 'transition_sequence'=>1,
              'items_json'=>JSON.generate({'items'=>[], 'capture_error'=>'source_missing'}) }
    last = first.merge('event_id'=>'two', 'operation'=>'modified', 'previous_event_id'=>'one', 'transition_sequence'=>2,
                       'previous_source_relative_path'=>'never.txt')
    deleted = last.merge('event_id'=>'three','operation'=>'deleted','status'=>'tombstoned','previous_event_id'=>'two','transition_sequence'=>3)
    [deleted,last,{'one'=>first,'two'=>last}]
  end

  def test_proven_never_captured_delete
    assert MemoryValidationContracts.deleted_before_capture?(*chain)
  end

  def test_all_unproven_histories_remain_invalid
    %w[current_sha256 previous_sha256 source_entry_id status items_json transition_sequence].each do |field|
      row,previous,events=chain
      events['one'][field]='invalid'
      refute MemoryValidationContracts.deleted_before_capture?(row,previous,events), field
    end
    row,previous,events=chain
    events.delete('one')
    refute MemoryValidationContracts.deleted_before_capture?(row,previous,events)
  end

  def test_blocked_is_valid_only_without_writes_or_batch
    run={'status'=>'blocked','kind'=>'incremental','error_code'=>'batch_limit','writes_count'=>0,'transaction_id'=>nil,
         'batch_path'=>nil,'batch_sha256'=>nil,'content_items'=>10,'total_bytes'=>1,'error_message'=>'bounded limit','created_at'=>Time.now.iso8601}
    assert MemoryValidationContracts.valid_blocked_run?(run)
    {'writes_count'=>1,'transaction_id'=>'present','batch_path'=>'present','batch_sha256'=>'hash','error_code'=>'other','created_at'=>'bad'}.each do |key,value|
      refute MemoryValidationContracts.valid_blocked_run?(run.merge(key=>value)), key
    end
  end
end
