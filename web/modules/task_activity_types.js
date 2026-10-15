/**
 * @typedef {Object} ActiveDirectTurn
 * @property {Object=} model_activity  // exact-call transport observation; no request/account content
 * @property {Object.<string,Object>=} model_waits
 * @property {number=} task_attempt
 * @property {string} activity_id
 * @property {number} chat_id
 * @property {string} project_id
 * @property {string} client_message_id
 * @property {string} kind
 * @property {string} phase
 * @property {number} started_at
 */
/** Shared census facts from the gateway; phase and outcome are independent. */
/**
 * @typedef {Object} ActiveChatActivity
 * @property {string=} status  // recorded lifecycle outcome; phase still owns current activity
 * @property {Object=} outcome_axes
 * @property {string=} reason_code
 * @property {Object=} root_phase_checkpoint  // compact post_task_synthesis fact only
 * @property {string=} timeout_retry_from  // explicit automatic-retry predecessor, never owner Continue
 * @property {string=} original_task_id
 * @property {Object=} owner_wait  // quiz-bound state, quiz_state and optional wait_ended_at, independent of Project detail
 * @property {Object=} project_admission_hold  // accepted unstarted work waiting for original Project authority
 * @property {string=} pause_cause  // budget | owner | restart | sleep | unknown; display only
 * @property {boolean=} finishing_reviews  // review work an owner Pause lets finish runs on; display only, no count
 * @property {Object=} late_work  // answered root with an open late phase (D10): title, answered_at; display only
 * @property {Object=} required_question  // read-only pointer to the current required Project quiz
 * @property {boolean=} required_question_unavailable  // a recorded owner-question wait whose detail could not be read: possibly blocked, never "no question"
 * @property {Object.<string,Object>=} model_waits
 * @property {Object=} model_activity  // exact-call transport observation; never quota wait or task liveness
 * @property {number=} task_attempt
 * @property {string} activity_id
 * @property {number} chat_id
 * @property {string} project_id
 * @property {string} client_message_id  // empty for managed queue rows
 * @property {string} kind  // direct_chat | managed_task — presentational label; membership in this census, not kind, decides liveness
 * @property {string} phase  // managed: queued | budget_pausing | budget_paused | working | finalizing | unknown; direct: preparing | working | thinking | unknown; parked direct turns retain ID/kind
 * @property {number} started_at
 */
/**
 * A check's recorded criterion verdict (`verify_and_record`, #1585): on the live
 * `tool_call_finished` frame as `verification`, inside `tool_result_meta` on the settled
 * `tool_call` row, and on a replayed settled tool-evidence observation. The call's own
 * `status`/`is_error` stay its execution outcome; exit, signal and timeout stay process facts.
 * @typedef {Object} ToolVerificationFact
 * @property {"pass"|"fail"} status  // the durable receipt's verdict
 * @property {string} contract_kind  // visible_verifier | explicit_command | explicit_metric
 * @property {string} expected_match  // substring | exact | exact_line | json_equals | bytes_equal
 * @property {boolean=} matched  // present only where a comparison was evaluated (never on timeout)
 * @property {string} check  // the receipt's check rendering, bounded for transport
 * @property {string} check_rendering
 * @property {string=} criterion_id
 * @property {string} criterion_source  // task_stated | agent_defined
 * @property {string} receipt_ts  // the receipt's own timestamp; no separate receipt id exists
 * @property {string=} detail  // the producer's sentence for a timeout or a bytes_equal comparison
 */
