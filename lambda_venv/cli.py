# Copyright (c) 2022 Samuel J. McKelvie
#
# MIT License - See LICENSE file accompanying this package.
#

"""Command-line interface for this package"""

import base64
from math import exp
from typing import (
    TYPE_CHECKING, Optional, Sequence, List, Union, Dict, TextIO, Mapping, MutableMapping,
    cast, Any, Iterator, Iterable, Tuple, ItemsView, ValuesView, KeysView, Type, IO )

import logging
import uuid
from .logging import logger

import os
import sys
import datetime
import argparse
import argcomplete # type: ignore[import]
import json
from base64 import b64encode, b64decode
import colorama # type: ignore[import]
from colorama import Fore, Back, Style
import subprocess
from io import TextIOWrapper
import yaml
from urllib.parse import urlparse, ParseResult
import ruamel.yaml # type: ignore[import]
from io import StringIO

from .exceptions import LambdaVenvError
from .internal_types import JsonableTypes, Jsonable, JsonableDict, JsonableList
from .version import __version__ as pkg_version
from .util import full_type, create_aws_session
from .s3_util import (
    S3Client,
    generate_presigned_s3_upload_post,
    upload_file_to_s3_with_signed_post,
    s3_upload_folder
  )
from boto3 import Session

def is_colorizable(stream: TextIO) -> bool:
  is_a_tty = hasattr(stream, 'isatty') and stream.isatty()
  return is_a_tty


class CmdExitError(RuntimeError):
  exit_code: int

  def __init__(self, exit_code: int, msg: Optional[str]=None):
    if msg is None:
      msg = f"Command exited with return code {exit_code}"
    super().__init__(msg)
    self.exit_code = exit_code

class ArgparseExitError(CmdExitError):
  pass

class NoExitArgumentParser(argparse.ArgumentParser):
  def exit(self, status=0, message=None):
    if message:
      self._print_message(message, sys.stderr)
    raise ArgparseExitError(status, message)


class CommandLineInterface:
  _argv: Optional[Sequence[str]]
  _parser: argparse.ArgumentParser
  _args: argparse.Namespace
  _cwd: str

  _raw_stdout: TextIO = sys.stdout
  _raw_stderr: TextIO = sys.stderr
  _raw: bool = False
  _compact: bool = False
  _output_file: Optional[str] = None
  _encoding: str = 'utf-8'

  _colorize_stdout: bool = False
  _colorize_stderr: bool = False

  _aws_session: Optional[Session] = None
  _s3: Optional[S3Client] = None

  def __init__(self, argv: Optional[Sequence[str]]=None):
    self._argv = argv

  def ocolor(self, codes: str) -> str:
    return codes if self._colorize_stdout else ""

  def ecolor(self, codes: str) -> str:
    return codes if self._colorize_stderr else ""

  @property
  def cwd(self) -> str:
    return self._cwd

  def abspath(self, path: str) -> str:
    return os.path.abspath(os.path.join(self.cwd, os.path.expanduser(path)))

  def get_aws_session(self) -> Session:
    if self._aws_session is None:
      self._aws_session = create_aws_session(profile_name=self._args.aws_profile, region_name=self._args.aws_region)
    return self._aws_session

  def get_s3(self) -> S3Client:
    if self._s3 is None:
      self._s3 = self.get_aws_session().client('s3')
    return self._s3

  def pretty_print(
        self,
        value: Jsonable,
        compact: Optional[bool]=None,
        colorize: Optional[bool]=None,
        raw: Optional[bool]=None,
      ):

    if raw is None:
      raw = self._raw
    if raw:
      if isinstance(value, str):
        self._raw_stdout.write(value)
        return

    if compact is None:
      compact = self._compact
    if colorize is None:
      colorize = True

    def emit_to(f: TextIO):
      final_colorize = colorize and ((f is sys.stdout and self._colorize_stdout) or (f is sys.stderr and self._colorize_stderr))

      if not final_colorize:
        if compact:
          json.dump(value, f, separators=(',', ':'), sort_keys=True)
        else:
          json.dump(value, f, indent=2, sort_keys=True)
        f.write('\n')
      else:
        jq_input = json.dumps(value, separators=(',', ':'), sort_keys=True)
        cmd = [ 'jq' ]
        if compact:
          cmd.append('-c')
        cmd.append('.')
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=f) as proc:
          proc.communicate(input=jq_input.encode('utf-8'))
          exit_code = proc.returncode
        if exit_code != 0:
          raise subprocess.CalledProcessError(exit_code, cmd)

    output_file = self._output_file
    if output_file is None:
      emit_to(sys.stdout)
    else:
      with open(output_file, "w", encoding=self._encoding) as f:
        emit_to(f)

  def cmd_bare(self) -> int:
    print("A command is required", file=sys.stderr)
    return 1

  def cmd_version(self) -> int:
    self.pretty_print(pkg_version)
    return 0

  def cmd_test(self) -> int:
    args = self._args

    print(f"Test command, args={vars(args)}")

    return 0

  def run(self) -> int:
    """Run the commandline tool with provided arguments

    Args:
        argv (Optional[Sequence[str]], optional):
            A list of commandline arguments (NOT including the program as argv[0]!),
            or None to use sys.argv[1:]. Defaults to None.

    Returns:
        int: The exit code that would be returned if this were run as a standalone command.
    """
    parser = argparse.ArgumentParser(description="AWS lambda virtualenv management tool.")

    # ======================= Main command

    self._parser = parser
    parser.add_argument('--traceback', "--tb", action='store_true', default=False,
                        help='Display detailed exception information')
    parser.add_argument('--loglevel', default='warning',
                        choices=['critical', 'error', 'warning', 'info', 'debug'],
                        help='Set the logging level. Default is "warning"')
    parser.add_argument('-M', '--monochrome', action='store_true', default=False,
                        help='Output to stdout/stderr in monochrome. Default is to colorize if stream is a compatible terminal')
    parser.add_argument('-c', '--compact', action='store_true', default=False,
                        help='Compact instead of pretty-printed output')
    parser.add_argument('-r', '--raw', action='store_true', default=False,
                        help='''Output raw strings and binary content directly, not json-encoded.
                                Values embedded in structured results are not affected.''')
    parser.add_argument('-o', '--output', dest="output_file", default=None,
                        help='Write output value to the specified file instead of stdout')
    parser.add_argument('--text-encoding', default='utf-8',
                        help='The encoding used for text. Default  is utf-8')
    parser.add_argument('-C', '--cwd', default='.',
                        help="Change the effective directory used to search for configuration")
    parser.add_argument('-p', '--aws-profile', default=None,
                        help='The AWS profile to use. Default is to use the default AWS settings')
    parser.add_argument('--aws-region', default=None,
                        help='The AWS region to use. Default is to use the default AWS region for the selected profile')
    parser.add_argument('-e', '--venv', dest='venv_dir', default=None,
                        help='The directory containing the virtualenv. By default')
    parser.set_defaults(func=self.cmd_bare)

    subparsers = parser.add_subparsers(
                        title='Commands',
                        description='Valid commands',
                        help='Additional help available with "lambda-venv <command-name> -h"')

    # ======================= version

    parser_version = subparsers.add_parser('version',
                            description='''Display version information. JSON-quoted string. If a raw string is desired, use -r.''')
    parser_version.set_defaults(func=self.cmd_version)

    # ======================= test

    parser_test = subparsers.add_parser('test', description="Run a simple test. For debugging only.  Will be removed.")
    parser_test.set_defaults(func=self.cmd_test)

    # =========================================================

    argcomplete.autocomplete(parser)
    try:
      args = parser.parse_args(self._argv)
    except ArgparseExitError as ex:
      return ex.exit_code
    logging.basicConfig(level=args.loglevel.upper())
    logLevel = logging.getLogger().level
    # Restrict loglevel of boto3 and urllib3 modules because they are very chatty
    # and it is hard to find our log messages amongst the noise
    for modname in [
      'botocore.hooks','botocore.parsers','botocore.auth','botocore.endpoint','botocore.httpsession',
      'botocore.loaders','botocore.retryhandler','botocore.utils','botocore.client',
      'botocore.session','botocore.handlers','botocore.awsrequest','botocore.regions','urllib3.connectionpool',
      's3transfer.utils','s3transfer.tasks','s3transfer.futures']:
      logging.getLogger(modname).setLevel(max(logLevel, logging.INFO))
    logging.getLogger('botocore.credentials').setLevel(max(logLevel, logging.WARNING))
    traceback: bool = args.traceback
    try:
      self._args = args
      self._raw_stdout = sys.stdout
      self._raw_stderr = sys.stderr
      self._raw = args.raw
      self._compact = args.compact
      self._output_file = args.output_file
      self._encoding = args.text_encoding
      monochrome: bool = args.monochrome
      if not monochrome:
        self._colorize_stdout = is_colorizable(sys.stdout)
        self._colorize_stderr = is_colorizable(sys.stderr)
        if self._colorize_stdout or self._colorize_stderr:
          colorama.init(wrap=False)
          if self._colorize_stdout:
            new_stream = colorama.AnsiToWin32(sys.stdout)
            if new_stream.should_wrap():
              sys.stdout = new_stream
          if self._colorize_stderr:
            new_stream = colorama.AnsiToWin32(sys.stderr)
            if new_stream.should_wrap():
              sys.stderr = new_stream
      self._cwd = os.path.abspath(os.path.expanduser(args.cwd))
      rc = args.func()
    except Exception as ex:
      if isinstance(ex, CmdExitError):
        rc = ex.exit_code
      else:
        rc = 1
      if rc != 0:
        if traceback:
          raise

        print(f"{self.ecolor(Fore.RED)}lambda-venv: error: {ex}{self.ecolor(Style.RESET_ALL)}", file=sys.stderr)
    return rc

  @property
  def args(self) -> argparse.Namespace:
    return self._args

def run(argv: Optional[Sequence[str]]=None) -> int:
  try:
    rc = CommandLineInterface(argv).run()
  except CmdExitError as ex:
    rc = ex.exit_code
  return rc

class CommandHandler:
  cli: CommandLineInterface
  args: argparse.Namespace

  def __init__(self, cli: CommandLineInterface):
    self.cli = cli
    self.args = cli.args

  def __call__(self) -> int:
    raise NotImplementedError(f"{full_type(self)} has not implemented __call__")
