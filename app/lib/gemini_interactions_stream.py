"""SSE framing and lifecycle checks for Interactions, independent of content assembly.

See https://ai.google.dev/gemini-api/docs/streaming and the official OpenAPI.
Optional event_id recovery tokens are distinct from lifecycle interaction IDs.
"""
from __future__ import annotations
import json
import math
from urllib.parse import urlsplit


def stateless_text_stream_scope(body, url):
    """Scope output validation independently of retrievable resource identity."""
    if (not isinstance(body, dict) or body.get('model') != 'gemini-3.7-flash'
            or body.get('store') is not False or body.get('stream') is not True
            or not isinstance(body.get('input'), str) or not body['input'].strip()
            or body.get('background', False) is not False
            or set(body) - {'model', 'input', 'store', 'stream', 'background', 'generation_config', 'system_instruction'}
            or 'system_instruction' in body and not isinstance(body['system_instruction'], str)
            or not isinstance(body.get('generation_config', {}), dict)
            or any(k in body.get('generation_config', {}) for k in ('tool_choice', 'response_modalities', 'response_format'))):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname == 'generativelanguage.googleapis.com'
                and parsed.port in (None, 443) and parsed.path == '/v1beta/interactions'
                and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError, AttributeError):
        return False


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate JSON member')
            result[key] = item
        return result
    def constant(_):
        raise ValueError('nonfinite JSON number')
    def floating(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('nonfinite JSON number')
        return number
    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)


def terminal_steps_match(terminal, streamed):
    """Compare observable output slots; thought internals need not be repeated.

    A terminal resource may omit steps entirely (handled by the caller). When
    present, its text and function calls must agree with what was streamed.
    Thought signatures/summaries are not a second public output stream.
    """
    def observable(steps):
        if not isinstance(steps, list):
            raise ValueError('invalid terminal steps')
        result = []
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError('invalid output step')
            kind = step.get('type')
            if kind == 'thought':
                continue
            if kind == 'model_output':
                content = step.get('content', [])
                if not isinstance(content, list) or any(
                        not isinstance(part, dict) or part.get('type') != 'text'
                        or not isinstance(part.get('text'), str) for part in content):
                    raise ValueError('unverified output content')
                result.append(('text', ''.join(part['text'] for part in content)))
            elif kind == 'function_call':
                if (not all(isinstance(step.get(key), str) and step[key].strip() for key in ('id', 'name'))
                        or not isinstance(step.get('arguments'), dict)):
                    raise ValueError('invalid function call')
                arguments = json.dumps(step['arguments'], sort_keys=True, separators=(',', ':'), allow_nan=False)
                result.append(('function_call', step['id'], step['name'], arguments))
            else:
                raise ValueError('unverified terminal step')
        return result
    try:
        return observable(terminal) == observable(streamed)
    except (ValueError, TypeError, RecursionError):
        return False


class InteractionStream:
    def __init__(self, model, *, allow_empty_resource_ids=False):
        self.model = model
        self.errors = []
        self.created = 0
        self.completed = 0
        self.done = 0
        self.events = 0
        self.lifecycle_events = 0
        self.missing_ids = 0
        self.identity = None
        self.identity_conflict = False
        self.optional_event_ids = 0
        self.steps = {}
        self.step_types = {}
        self.observed_model = False
        self.raw_lines = []
        self.allow_empty_resource_ids = allow_empty_resource_ids
        self.empty_ids = 0
        self.text_deltas = 0
        self.usage_verified = False

    def fail(self, code):
        if code not in self.errors:
            self.errors.append(code)

    def _identity(self, event):
        self.lifecycle_events += 1
        resource = event.get('interaction')
        ident = resource.get('id') if isinstance(resource, dict) else event.get('interaction_id')
        if not isinstance(ident, str) or not ident.strip():
            self.missing_ids += 1
            present = 'id' in resource if isinstance(resource, dict) else 'interaction_id' in event
            if self.allow_empty_resource_ids and present and type(ident) is str and ident == '':
                self.empty_ids += 1
                if self.identity is not None:
                    self.fail('interaction_stream_mixed_resource_ids')
            else:
                self.fail('interaction_stream_identity_unverified')
        elif self.identity is None:
            self.identity = ident
            if self.empty_ids:
                self.fail('interaction_stream_mixed_resource_ids')
        elif self.identity != ident:
            self.identity_conflict = True
            self.fail('interaction_stream_identity_conflict')
        if isinstance(resource, dict) and 'model' in resource:
            self.observed_model = True
            if resource['model'] != self.model:
                self.fail('interaction_stream_model_mismatch')

    def observe(self, event):
        self.events += 1
        kind = event['event_type']
        if isinstance(event.get('event_id'), str) and event['event_id'].strip():
            self.optional_event_ids += 1
        if kind.startswith('interaction.'):
            self._identity(event)
        if self.completed:
            self.fail('interaction_stream_event_after_terminal')
        if kind == 'interaction.created':
            self.created += 1
            if self.created != 1 or self.events != 1:
                self.fail('interaction_stream_created_order')
            resource = event.get('interaction')
            if not isinstance(resource, dict) or resource.get('status') != 'in_progress':
                self.fail('interaction_stream_created_invalid')
        elif not self.created:
            self.fail('interaction_stream_missing_created')
        if kind.startswith('step.'):
            index = event.get('index', event.get('step_index'))
            if type(index) is not int or index < 0:
                self.fail('interaction_stream_step_index_invalid')
            elif kind == 'step.start':
                if index in self.steps or not isinstance(event.get('step'), dict) or not event['step'].get('type'):
                    self.fail('interaction_stream_step_start_invalid')
                else:
                    self.steps[index] = 'open'
                    self.step_types[index] = event['step']['type']
                    if self.step_types[index] not in ('model_output', 'thought', 'function_call'):
                        self.fail('interaction_stream_content_unverified')
            elif kind in ('step.delta', 'step.stop'):
                if self.steps.get(index) != 'open':
                    self.fail('interaction_stream_step_order')
                if kind == 'step.stop':
                    self.steps[index] = 'closed'
                elif not isinstance(event.get('delta'), dict):
                    self.fail('interaction_stream_delta_invalid')
                else:
                    allowed = {'model_output': {'text'}, 'thought': {'thought_signature', 'thought_summary', 'thought'},
                               'function_call': {'arguments', 'arguments_delta'}}
                    delta = event['delta']
                    if delta.get('type') not in allowed.get(self.step_types.get(index), set()):
                        self.fail('interaction_stream_content_unverified')
                    if delta.get('type') == 'text' and not isinstance(delta.get('text'), str):
                        self.fail('interaction_stream_delta_invalid')
                    elif delta.get('type') == 'text' and delta.get('text'):
                        self.text_deltas += 1
        if kind in ('interaction.completed', 'interaction.failed', 'interaction.cancelled', 'interaction.incomplete'):
            self.completed += 1
            if any(state != 'closed' for state in self.steps.values()):
                self.fail('interaction_stream_unclosed_step')
            if kind != 'interaction.completed':
                self.fail(kind.replace('.', '_'))
            resource = event.get('interaction')
            usage = resource.get('usage') if isinstance(resource, dict) else None
            fields = ('total_input_tokens', 'total_output_tokens', 'total_thought_tokens', 'total_tokens')
            self.usage_verified = (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in fields)
                                   and usage['total_tokens'] == sum(usage[k] for k in fields[:3]))
            if not self.usage_verified:
                self.fail('interaction_stream_usage_invalid')
        if kind == 'error':
            self.fail('interaction_stream_error')

    def payloads(self, lines):
        """Yield complete data frames; never dispatch a partial frame at EOF."""
        data = []
        event_name = None
        frame_size = total_size = 0

        def dispatch():
            nonlocal event_name, frame_size
            payload = '\n'.join(data)
            data.clear()
            name, event_name = event_name, None
            frame_size = 0
            if not payload:
                return None
            if self.done:
                self.fail('interaction_stream_data_after_done')
                if payload == '[DONE]':
                    self.done += 1
                return None
            if payload == '[DONE]':
                self.done += 1
                if self.completed != 1:
                    self.fail('interaction_stream_done_before_terminal')
                return payload
            try:
                event = strict_json(payload)
                if not isinstance(event, dict) or not isinstance(event.get('event_type'), str) or not event['event_type']:
                    raise ValueError('invalid event envelope')
            except (ValueError, TypeError, RecursionError):
                self.fail('interaction_stream_json_invalid')
                return None
            if name not in (None, 'message', event['event_type']):
                self.fail('interaction_stream_event_name_mismatch')
            self.observe(event)
            return payload

        for raw in lines:
            if not isinstance(raw, (bytes, str)):
                self.fail('interaction_stream_utf8_invalid')
                break
            # iter_lines removes LF/CRLF. Account conservatively for CRLF and
            # charge raw bytes before decoding, including invalid UTF-8/blank
            # lines, so malformed input cannot bypass either byte budget.
            size = len(raw) if isinstance(raw, bytes) else len(raw.encode('utf-8', errors='replace'))
            frame_size += size + 2
            total_size += size + 2
            if frame_size > 2 * 1024 * 1024 or total_size > 8 * 1024 * 1024:
                self.fail('interaction_stream_byte_limit')
                break
            try:
                line = raw.decode('utf-8', errors='strict') if isinstance(raw, bytes) else raw
                line.encode('utf-8', errors='strict')
            except UnicodeError:
                self.fail('interaction_stream_utf8_invalid')
                if isinstance(raw, bytes):
                    self.raw_lines.append(raw.decode('utf-8', errors='replace'))
                continue
            self.raw_lines.append(line)
            if line == '':
                payload = dispatch()
                if payload is not None:
                    yield payload
            elif line.startswith(':'):
                continue
            else:
                field, _, value = line.partition(':')
                value = value.removeprefix(' ')
                if field == 'data':
                    data.append(value)
                elif field == 'event':
                    event_name = value
                # id and retry are SSE metadata; future unknown fields are ignored.
        if data:
            self.fail('interaction_stream_incomplete_frame')
        if self.created == 0:
            self.fail('interaction_stream_missing_created')
        if self.completed != 1:
            self.fail('interaction_stream_missing_terminal')
        if self.done == 0:
            self.fail('stream_missing_done')
        elif self.done > 1:
            self.fail('interaction_stream_done_count_invalid')
        if not self.observed_model:
            self.fail('interaction_stream_model_unverified')
        if self.empty_ids:
            if self.empty_ids != self.lifecycle_events:
                self.fail('interaction_stream_mixed_resource_ids')
            if not self.text_deltas or any(kind not in ('thought', 'model_output') for kind in self.step_types.values()):
                self.fail('interaction_stream_stateless_text_unverified')

    def diagnostics(self):
        return {'errors': list(self.errors), 'event_count': self.events,
                'created_count': self.created, 'terminal_count': self.completed, 'done_count': self.done,
                'lifecycle_event_count': self.lifecycle_events,
                'lifecycle_identity_unobservable_count': self.missing_ids,
                'lifecycle_identity_verified': bool(self.identity) and not self.missing_ids and not self.identity_conflict,
                'resource_identity_verified': bool(self.identity) and not self.missing_ids and not self.identity_conflict,
                'all_lifecycle_ids_exactly_empty': bool(self.lifecycle_events) and self.empty_ids == self.lifecycle_events,
                'stateless_source_scope_verified': self.allow_empty_resource_ids,
                'stream_output_usage_verified': not self.errors and self.usage_verified and self.text_deltas > 0,
                'resume_verification': 'not_tested',
                'event_id_observed_count': self.optional_event_ids, 'event_id_required': False}
