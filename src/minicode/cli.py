import argparse
import difflib
import getpass
import json
import os
import platform
import readline  # noqa: F401
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import httpx
import jsonschema
import openai
import tavily

RESET = '\033[0m'
BOLD = '\033[1m'
RED = '\033[31m'
GREEN = '\033[32m'
ORANGE = '\033[33m'
CYAN = '\033[36m'
GRAY = '\033[90m'


def confirm(message, default=True):
    prompt = f'{message} [{"Y/n" if default else "y/N"}] '

    while True:
        user_input = input(prompt).strip().lower()

        if user_input in ('y', 'n', ''):
            if user_input == '':
                return default
            return user_input == 'y'


class Diff:
    def __init__(self, old_content, new_content, old_file, new_file):
        self._diff = list(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=old_file,
            tofile=new_file,
        ))

    def plain(self):
        return ''.join(self._diff)

    def colorized(self):
        result_lines = []

        for line in self._diff:
            if line.startswith('---') or line.startswith('+++'):
                result_lines.append(f'{BOLD}{line}{RESET}')
            elif line.startswith('@@'):
                result_lines.append(f'{CYAN}{line}{RESET}')
            elif line.startswith('-'):
                result_lines.append(f'{RED}{line}{RESET}')
            elif line.startswith('+'):
                result_lines.append(f'{GREEN}{line}{RESET}')
            else:
                result_lines.append(line)

        return ''.join(result_lines)


class ToolError(Exception):
    pass


class BashTool:
    definition = {
        'type': 'function',
        'function': {
            'name': 'Bash',
            'description': 'Execute a bash command.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {
                        'type': 'string',
                    },
                    'cwd': {
                        'type': 'string',
                        'description': 'Absolute path to the working directory.',
                    },
                    'timeout': {
                        'type': 'number',
                        'minimum': 1,
                        'default': 60,
                        'description': 'Timeout in seconds.',
                    },
                },
                'required': ['command', 'cwd'],
                'additionalProperties': False,
            },
        },
    }

    def __init__(self, bash_path='/bin/bash', skip_permissions=False):
        self._bash_path = bash_path
        self._skip_permissions = skip_permissions

    def __call__(self, command, cwd, timeout=60):
        if not os.path.isabs(cwd):
            raise ToolError('cwd must be an absolute path.')

        if not os.path.exists(cwd):
            raise ToolError('cwd does not exist.')

        if not os.path.isdir(cwd):
            raise ToolError('cwd is not a directory.')

        print(command, flush=True)

        if cwd != os.getcwd():
            print(f'(in {cwd})', flush=True)

        if not self._skip_permissions:
            if not confirm('❓ Do you want to proceed?'):
                user_input = input('📝 Tell the assistant what to do differently (optional): ')

                if user_input:
                    raise ToolError(f'User rejected with message: {user_input}')
                else:
                    raise ToolError('User rejected.')

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                executable=self._bash_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd,
            )

            output_lines = []

            def read_stream(stream, collected):
                for line in stream:
                    print(f'{GRAY}{line}{RESET}', end='', flush=True)
                    collected.append(line)

            thread = threading.Thread(target=read_stream, args=(process.stdout, output_lines))
            thread.start()

            process.wait(timeout=timeout)
            thread.join()
        except subprocess.TimeoutExpired:
            process.kill()
            thread.join()

            raise ToolError(f'Timed out after {timeout} second(s).')

        output_content = ''.join(output_lines)

        if len(output_content) > 65536:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False, suffix='.txt') as f:
                f.write(output_content)
                output_file_path = f.name

            output = {
                'file_path': output_file_path,
                'total_lines': len(output_lines),
                'total_chars': len(output_content),
            }
        else:
            output = {'content': output_content}

        print(f'✅ {GREEN}Success{RESET}', flush=True)

        return {
            'output': output,
            'exit_code': process.returncode,
        }


class ReadTool:
    definition = {
        'type': 'function',
        'function': {
            'name': 'Read',
            'description': '''Read a file from the local filesystem. \
Results are returned using cat -n format, with line numbers starting at 1.''',
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_path': {
                        'type': 'string',
                        'description': 'Absolute path to the file.',
                    },
                    'offset': {
                        'type': 'integer',
                        'minimum': 1,
                        'default': 1,
                        'description': 'Line number to start reading from.',
                    },
                    'limit': {
                        'type': 'integer',
                        'minimum': 1,
                        'default': 1000,
                        'description': 'Maximum number of lines to read.',
                    },
                    'chars_limit': {
                        'type': 'integer',
                        'minimum': 1,
                        'default': 65536,
                        'description': 'Maximum number of characters to read.',
                    },
                },
                'required': ['file_path'],
                'additionalProperties': False,
            },
        },
    }

    def __init__(self, skip_permissions=False):
        self._skip_permissions = skip_permissions

    def __call__(self, file_path, offset=1, limit=1000, chars_limit=65536):
        if not os.path.isabs(file_path):
            raise ToolError('file_path must be an absolute path.')

        if not os.path.exists(file_path):
            raise ToolError('file_path does not exist.')

        if not os.path.isfile(file_path):
            raise ToolError('file_path is not a regular file.')

        if not os.access(file_path, os.R_OK):
            raise ToolError('file_path is not readable.')

        with open(file_path, mode='r', encoding='utf-8') as f:
            full_lines = f.readlines()

        full_content = ''.join(full_lines)

        lines = full_lines[offset - 1:]

        if len(lines) > limit:
            lines = lines[:limit] + [f'... (truncated to {limit} lines)\n']

        content = ''.join(lines)

        if len(content) > chars_limit:
            content = content[:chars_limit] + f'... (truncated to {chars_limit} chars)\n'

        result_content = ''.join(f'{offset+i:>6}\t{line}' for i, line in enumerate(content.splitlines(keepends=True)))

        print(f'{file_path} (lines {offset}-{min(offset + limit - 1, len(full_lines))})', flush=True)

        if not self._skip_permissions:
            if not confirm('❓ Do you want to proceed?'):
                user_input = input('📝 Tell the assistant what to do differently (optional): ')

                if user_input:
                    raise ToolError(f'User rejected with message: {user_input}')
                else:
                    raise ToolError('User rejected.')

        print(f'✅ {GREEN}Success{RESET}', flush=True)

        return {
            'content': result_content,
            'total_lines': len(full_lines),
            'total_chars': len(full_content),
        }


class WriteTool:
    definition = {
        'type': 'function',
        'function': {
            'name': 'Write',
            'description': 'Write a file to the local filesystem.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_path': {
                        'type': 'string',
                        'description': 'Absolute path to the file.',
                    },
                    'overwrite': {
                        'type': 'boolean',
                        'description': 'Whether to overwrite the file if it already exists.',
                    },
                    'content': {
                        'type': 'string',
                    },
                },
                'required': ['file_path', 'overwrite', 'content'],
                'additionalProperties': False,
            },
        },
    }

    def __init__(self, skip_permissions=False):
        self._skip_permissions = skip_permissions

    def __call__(self, file_path, overwrite, content):
        if not os.path.isabs(file_path):
            raise ToolError('file_path must be an absolute path.')

        parent_dir = os.path.dirname(file_path) or '.'

        if not os.path.exists(parent_dir):
            raise ToolError('dirname(file_path) does not exist.')

        if not os.access(parent_dir, os.W_OK):
            raise ToolError('dirname(file_path) is not writable.')

        if not os.path.exists(file_path):
            diff = Diff(
                old_content='',
                new_content=content,
                old_file='/dev/null',
                new_file=file_path,
            )
        else:
            if not overwrite:
                raise ToolError('file_path already exists and overwrite is false.')

            if not os.path.isfile(file_path):
                raise ToolError('file_path already exists and is not a regular file.')

            if not os.access(file_path, os.W_OK):
                raise ToolError('file_path already exists and is not writable.')

            old_content = ''

            if os.access(file_path, os.R_OK):
                with open(file_path, mode='r', encoding='utf-8') as f:
                    old_content = f.read()

            diff = Diff(
                old_content=old_content,
                new_content=content,
                old_file=file_path,
                new_file=file_path,
            )

        print(diff.colorized(), flush=True)

        if not self._skip_permissions:
            if not confirm('❓ Do you want to proceed?'):
                user_input = input('📝 Tell the assistant what to do differently (optional): ')

                if user_input:
                    raise ToolError(f'User rejected with message: {user_input}')
                else:
                    raise ToolError('User rejected.')

        with open(file_path, mode='w', encoding='utf-8') as f:
            f.write(content)

        print(f'✅ {GREEN}Success{RESET}', flush=True)

        return {'success': True}


class EditTool:
    definition = {
        'type': 'function',
        'function': {
            'name': 'Edit',
            'description': 'Perform exact string replacements in files.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'file_path': {
                        'type': 'string',
                        'description': 'Absolute path to the file.',
                    },
                    'old_string': {
                        'type': 'string',
                        'description': 'The text to replace. Must be unique in the file.',
                    },
                    'new_string': {
                        'type': 'string',
                        'description': 'The text to replace with.',
                    },
                },
                'required': ['file_path', 'old_string', 'new_string'],
                'additionalProperties': False,
            },
        },
    }

    def __init__(self, skip_permissions=False):
        self._skip_permissions = skip_permissions

    def __call__(self, file_path, old_string, new_string):
        if not os.path.isabs(file_path):
            raise ToolError('file_path must be an absolute path.')

        if not os.path.exists(file_path):
            raise ToolError('file_path does not exist.')

        if not os.path.isfile(file_path):
            raise ToolError('file_path is not a regular file.')

        if not os.access(file_path, os.R_OK):
            raise ToolError('file_path is not readable.')

        if not os.access(file_path, os.W_OK):
            raise ToolError('file_path is not writable.')

        with open(file_path, mode='r', encoding='utf-8') as f:
            old_content = f.read()

        if old_string not in old_content:
            raise ToolError('old_string not found in file.')

        if old_content.count(old_string) > 1:
            raise ToolError('old_string appears multiple times in file. It must be unique.')

        new_content = old_content.replace(old_string, new_string, 1)

        diff = Diff(
            old_content=old_content,
            new_content=new_content,
            old_file=file_path,
            new_file=file_path,
        )

        print(diff.colorized(), flush=True)

        if not self._skip_permissions:
            if not confirm('❓ Do you want to proceed?'):
                user_input = input('📝 Tell the assistant what to do differently (optional): ')

                if user_input:
                    raise ToolError(f'User rejected with message: {user_input}')
                else:
                    raise ToolError('User rejected.')

        with open(file_path, mode='w', encoding='utf-8') as f:
            f.write(new_content)

        print(f'✅ {GREEN}Success{RESET}', flush=True)

        return {
            'success': True,
            'diff': diff.plain(),
        }


class WebFetchTool:
    definition = {
        'type': 'function',
        'function': {
            'name': 'WebFetch',
            'description': 'Fetch content from a specified URL.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {
                        'type': 'string',
                    },
                },
                'required': ['url'],
                'additionalProperties': False,
            },
        },
    }

    def __init__(self, client, skip_permissions=False):
        self._client = client
        self._skip_permissions = skip_permissions

    def __call__(self, url):
        print(url, flush=True)

        if not self._skip_permissions:
            if not confirm('❓ Do you want to proceed?'):
                user_input = input('📝 Tell the assistant what to do differently (optional): ')

                if user_input:
                    raise ToolError(f'User rejected with message: {user_input}')
                else:
                    raise ToolError('User rejected.')

        response = self._client.extract(urls=[url], extract_depth='advanced')

        title = response['results'][0]['title']
        content = response['results'][0]['raw_content']

        if len(content) > 65536:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False, suffix='.txt') as f:
                f.write(content)
                file_path = f.name

            result = {
                'title': title,
                'file_path': file_path,
            }
        else:
            result = {
                'title': title,
                'content': content,
            }

        print(f'✅ {GREEN}Success{RESET}', flush=True)
        return result


class WebSearchTool:
    definition = {
        'type': 'function',
        'function': {
            'name': 'WebSearch',
            'description': 'Search the web.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                    },
                    'max_results': {
                        'type': 'integer',
                    },
                },
                'required': ['query', 'max_results'],
                'additionalProperties': False,
            },
        },
    }

    def __init__(self, client, skip_permissions=False):
        self._client = client
        self._skip_permissions = skip_permissions

    def __call__(self, query, max_results):
        print(query, flush=True)

        if not self._skip_permissions:
            if not confirm('❓ Do you want to proceed?'):
                user_input = input('📝 Tell the assistant what to do differently (optional): ')

                if user_input:
                    raise ToolError(f'User rejected with message: {user_input}')
                else:
                    raise ToolError('User rejected.')

        response = self._client.search(query=query, max_results=max_results)
        print(f'✅ {GREEN}Success{RESET}', flush=True)
        return response['results']


def compact(messages):
    if len(messages) <= 3:
        return messages

    compacted = []

    compacted.append(messages[0])

    recent_count = (len(messages) - 1) // 2

    for message in messages[1:-recent_count]:
        if message['role'] == 'tool':
            compacted.append({
                'role': 'tool',
                'tool_call_id': message['tool_call_id'],
                'content': '(content removed to save space in the context window)',
            })
        else:
            compacted.append(message)

    compacted.extend(messages[-recent_count:])

    return compacted


def main():
    parser = argparse.ArgumentParser(description='MiniCode: A command-line AI coding agent.')
    parser.add_argument('--skip-permissions', action='store_true', help='Bypass all permission checks.')
    args = parser.parse_args()

    try:
        print('Welcome to MiniCode! Press Ctrl+C or Ctrl+D to exit.', flush=True)

        if 'OPENAI_BASE_URL' not in os.environ:
            user_input = input('📝 OpenAI Base URL [https://api.openai.com/v1]: ').strip()
            os.environ['OPENAI_BASE_URL'] = user_input or 'https://api.openai.com/v1'

        if 'OPENAI_API_KEY' not in os.environ:
            user_input = getpass.getpass('📝 OpenAI API Key: ')

            if not user_input:
                print(f'❌ {RED}Error: OPENAI_API_KEY is required.{RESET}', flush=True)
                return 1

            os.environ['OPENAI_API_KEY'] = user_input

        if 'MINICODE_MODEL' not in os.environ:
            user_input = input('📝 Model: ').strip()

            if not user_input:
                print(f'❌ {RED}Error: MINICODE_MODEL is required.{RESET}', flush=True)
                return 1

            os.environ['MINICODE_MODEL'] = user_input

        if 'MINICODE_CONTEXT_WINDOW' not in os.environ:
            user_input = input('📝 Context Window [128000]: ').strip()
            os.environ['MINICODE_CONTEXT_WINDOW'] = user_input or '128000'

        try:
            int(os.environ['MINICODE_CONTEXT_WINDOW'])
        except ValueError:
            print(f'❌ {RED}Error: MINICODE_CONTEXT_WINDOW must be an integer.{RESET}', flush=True)
            return 1

        if 'TAVILY_BASE_URL' not in os.environ:
            user_input = input('📝 Tavily Base URL [https://api.tavily.com]: ').strip()
            os.environ['TAVILY_BASE_URL'] = user_input or 'https://api.tavily.com'

        if 'TAVILY_API_KEY' not in os.environ:
            user_input = getpass.getpass('📝 Tavily API Key (optional): ')
            os.environ['TAVILY_API_KEY'] = user_input

        if not os.environ['TAVILY_API_KEY']:
            print(f'⚠️ {ORANGE}Warning: TAVILY_API_KEY not set. WebFetch and WebSearch tools will be unavailable.{RESET}', flush=True)

        openai_client = openai.OpenAI(
            base_url=os.environ['OPENAI_BASE_URL'],
            api_key=os.environ['OPENAI_API_KEY'],
            timeout=20.,
            max_retries=0,
        )

        bash_path = shutil.which('bash') or '/bin/bash'

        tools = {
            'Bash': BashTool(bash_path=bash_path, skip_permissions=args.skip_permissions),
            'Read': ReadTool(skip_permissions=args.skip_permissions),
            'Write': WriteTool(skip_permissions=args.skip_permissions),
            'Edit': EditTool(skip_permissions=args.skip_permissions),
        }

        if os.environ['TAVILY_API_KEY']:
            tavily_client = tavily.TavilyClient(
                api_base_url=os.environ['TAVILY_BASE_URL'],
                api_key=os.environ['TAVILY_API_KEY'],
            )
            tools.update({
                'WebFetch': WebFetchTool(client=tavily_client, skip_permissions=args.skip_permissions),
                'WebSearch': WebSearchTool(client=tavily_client, skip_permissions=args.skip_permissions),
            })

        system_prompt = f'''\
You are MiniCode, a powerful command-line AI coding agent.

System: {platform.system()}
Working Directory: {os.getcwd()}'''

        messages = [
            {'role': 'system', 'content': system_prompt},
        ]

        while True:
            user_input = input('> ').strip()

            if not user_input:
                continue

            messages.append({
                'role': 'user',
                'content': user_input,
            })

            try:
                while True:
                    try:
                        retries_left = 5
                        delay = 0.5

                        while True:
                            try:
                                stream = openai_client.chat.completions.create(
                                    model=os.environ['MINICODE_MODEL'],
                                    messages=messages,
                                    tools=[tool.definition for tool in tools.values()],
                                    stream=True,
                                    stream_options={'include_usage': True},
                                )

                                has_reasoning_content = False
                                reasoning_content = ''
                                has_reasoning_details = False
                                reasoning_details = []
                                content = ''
                                tool_calls = []
                                finish_reason = None
                                usage = None

                                for chunk in stream:
                                    if not chunk.choices:
                                        continue

                                    delta = chunk.choices[0].delta

                                    if not hasattr(delta, 'reasoning_content'):
                                        if hasattr(delta, 'reasoning'):
                                            delta.reasoning_content = delta.reasoning

                                    delta_reasoning_content = getattr(delta, 'reasoning_content', None)

                                    if delta_reasoning_content is not None:
                                        print(f'{GRAY}{delta_reasoning_content}{RESET}', end='', flush=True)
                                        has_reasoning_content = True
                                        reasoning_content += delta_reasoning_content

                                    delta_reasoning_details = getattr(delta, 'reasoning_details', None)

                                    if delta_reasoning_details is not None:
                                        has_reasoning_details = True
                                        reasoning_details.extend(delta_reasoning_details)

                                    delta_content = getattr(delta, 'content', None)

                                    if delta_content:
                                        if not content:
                                            if reasoning_content:
                                                print('\n\n', end='', flush=True)

                                        print(delta_content, end='', flush=True)
                                        content += delta_content

                                    delta_tool_calls = getattr(delta, 'tool_calls', None)

                                    if delta_tool_calls:
                                        for tool_call in delta_tool_calls:
                                            tool_call_id = getattr(tool_call, 'id', None)

                                            if tool_call_id is not None:
                                                print('\n\n', end='', flush=True)

                                                tool_calls.append({
                                                    'id': tool_call_id,
                                                    'name': '',
                                                    'arguments': '',
                                                })

                                            name = getattr(tool_call.function, 'name', None)

                                            if name is not None:
                                                print(f'{GRAY}{name}{RESET}', end='', flush=True)
                                                tool_calls[-1]['name'] += name

                                            arguments = getattr(tool_call.function, 'arguments', None)

                                            if arguments is not None:
                                                print(f'{GRAY}{arguments}{RESET}', end='', flush=True)
                                                tool_calls[-1]['arguments'] += arguments

                                    if chunk.choices[0].finish_reason is not None:
                                        finish_reason = chunk.choices[0].finish_reason

                                    if chunk.usage is not None:
                                        usage = chunk.usage

                                print('\n', end='', flush=True)
                                break
                            except (
                                httpx.HTTPError,
                                openai.APIConnectionError,
                                openai.APITimeoutError,
                                openai.ConflictError,
                                openai.InternalServerError,
                                openai.RateLimitError,
                                openai.UnprocessableEntityError,
                            ) as e:
                                retries_left -= 1

                                if retries_left == 0:
                                    raise

                                print('\n', end='', flush=True)
                                print(f'⚠️ {ORANGE}Warning: {repr(e)} ({retries_left} retries left){RESET}', flush=True)

                                time.sleep(delay)
                                delay *= 2
                    except (httpx.HTTPError, openai.APIError) as e:
                        print('\n', end='', flush=True)
                        print(f'❌ {RED}Error: {repr(e)}{RESET}', flush=True)
                        break

                    message = {
                        'role': 'assistant',
                        'content': content,
                        'tool_calls': [
                            {
                                'id': tool_call['id'],
                                'type': 'function',
                                'function': {
                                    'name': tool_call['name'],
                                    'arguments': tool_call['arguments'],
                                },
                            }
                            for tool_call in tool_calls
                        ],
                    }

                    # https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#preserving-reasoning
                    # https://api-docs.deepseek.com/guides/thinking_mode#compatibility-notice
                    # https://docs.z.ai/guides/capabilities/thinking-mode
                    # https://platform.moonshot.ai/docs/guide/use-kimi-k2-thinking-model#multi-step-tool-call

                    if has_reasoning_details:
                        message['reasoning_details'] = reasoning_details

                    if has_reasoning_content:
                        message['reasoning_content'] = reasoning_content

                    messages.append(message)

                    if finish_reason not in ('stop', 'tool_calls', 'function_call'):
                        print(f'⚠️ {ORANGE}Warning: finish_reason={finish_reason}{RESET}', flush=True)

                    if usage is not None:
                        tokens = getattr(usage, 'total_tokens', 0)
                        percentage = (tokens / int(os.environ['MINICODE_CONTEXT_WINDOW'])) * 100

                        print(f'📊 {tokens:,} tokens ({percentage:.2f}%)', flush=True)

                        if percentage > 80:
                            messages = compact(messages)

                            print(f'⚠️ {ORANGE}Warning: Context compacted.{RESET}', flush=True)

                    if not tool_calls:
                        break

                    for tool_call in tool_calls:
                        try:
                            try:
                                tool = tools[tool_call['name']]
                            except KeyError:
                                raise ToolError('Unknown tool.')

                            try:
                                arguments = json.loads(tool_call['arguments'])
                            except json.JSONDecodeError as e:
                                raise ToolError(f'Invalid arguments: {e}')

                            try:
                                jsonschema.validate(arguments, tool.definition['function']['parameters'])
                            except jsonschema.ValidationError as e:
                                raise ToolError(f'Invalid arguments: {e.message}')

                            print('\n', end='', flush=True)
                            print(f'🔧 {tool.definition["function"]["name"]}', flush=True)

                            result = tool(**arguments)

                            messages.append({
                                'role': 'tool',
                                'tool_call_id': tool_call['id'],
                                'content': json.dumps(result),
                            })
                        except (KeyboardInterrupt, EOFError):
                            raise
                        except ToolError as e:
                            print(f'❌ {RED}Error: {e}{RESET}', flush=True)

                            messages.append({
                                'role': 'tool',
                                'tool_call_id': tool_call['id'],
                                'content': json.dumps({'error': f'{e}'}),
                            })
                        except Exception as e:
                            print(f'❌ {RED}Error: {repr(e)}{RESET}', flush=True)

                            messages.append({
                                'role': 'tool',
                                'tool_call_id': tool_call['id'],
                                'content': json.dumps({'error': repr(e)}),
                            })
            except (KeyboardInterrupt, EOFError):
                print('\n', end='', flush=True)
                print(f'🚫 {RED}Interrupted{RESET}')
    except (KeyboardInterrupt, EOFError):
        print('\n', end='', flush=True)

    return 0


if __name__ == '__main__':
    sys.exit(main())
