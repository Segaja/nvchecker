# MIT licensed
# Copyright (c) 2013-2020 lilydjwg <lilydjwg@gmail.com>, et al.

import time
from urllib.parse import urlencode
from typing import List, Tuple, Union

import structlog

from nvchecker.api import (
  VersionResult, Entry, AsyncCache, KeyManager,
  TemporaryError, session, RichResult, GetVersionError,
)

logger = structlog.get_logger(logger_name=__name__)

GITHUB_URL = 'https://api.github.com/repos/%s/commits'
GITHUB_LATEST_RELEASE = 'https://api.github.com/repos/%s/releases/latest'
# https://developer.github.com/v3/git/refs/#get-all-references
GITHUB_MAX_TAG = 'https://api.github.com/repos/%s/git/refs/tags'
GITHUB_GRAPHQL_URL = 'https://api.github.com/graphql'

async def get_version(name, conf, **kwargs):
  try:
    return await get_version_real(name, conf, **kwargs)
  except TemporaryError as e:
    check_ratelimit(e, name)

QUERY_LATEST_TAG = '''
{{
  repository(name: "{name}", owner: "{owner}") {{
    refs(refPrefix: "refs/tags/", first: 1,
         query: "{query}",
         orderBy: {{field: TAG_COMMIT_DATE, direction: DESC}}) {{
      edges {{
        node {{
          name
        }}
      }}
    }}
  }}
}}
'''

QUERY_LATEST_RELEASE_WITH_PRERELEASES = '''
{{
  repository(name: "{name}", owner: "{owner}") {{
    releases(first: 1, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      edges {{
        node {{
          name
          url
        }}
      }}
    }}
  }}
}}
'''

async def get_latest_tag(key: Tuple[str, str, str]) -> RichResult:
  repo, query, token = key
  owner, reponame = repo.split('/')
  headers = {
    'Authorization': f'bearer {token}',
    'Content-Type': 'application/json',
  }
  q = QUERY_LATEST_TAG.format(
    owner = owner,
    name = reponame,
    query = query,
  )

  res = await session.post(
    GITHUB_GRAPHQL_URL,
    headers = headers,
    json = {'query': q},
  )
  j = res.json()

  refs = j['data']['repository']['refs']['edges']
  if not refs:
    raise GetVersionError('no tag found')

  version = refs[0]['node']['name']
  return RichResult(
    version = version,
    url = f'https://github.com/{repo}/releases/tag/{version}',
  )

async def get_latest_release_with_prereleases(key: Tuple[str, str]) -> RichResult:
  repo, token = key
  owner, reponame = repo.split('/')
  headers = {
    'Authorization': f'bearer {token}',
    'Content-Type': 'application/json',
  }
  q = QUERY_LATEST_RELEASE_WITH_PRERELEASES.format(
    owner = owner,
    name = reponame,
  )

  res = await session.post(
    GITHUB_GRAPHQL_URL,
    headers = headers,
    json = {'query': q},
  )
  j = res.json()

  refs = j['data']['repository']['releases']['edges']
  if not refs:
    raise GetVersionError('no release found')

  return RichResult(
    version = refs[0]['node']['name'],
    url = refs[0]['node']['url'],
  )

async def get_version_real(
  name: str, conf: Entry, *,
  cache: AsyncCache, keymanager: KeyManager,
  **kwargs,
) -> VersionResult:
  repo = conf['github']

  # Load token from config
  token = conf.get('token')
  # Load token from keyman
  if token is None:
    token = keymanager.get_key('github')

  use_latest_tag = conf.get('use_latest_tag', False)
  if use_latest_tag:
    if not token:
      raise GetVersionError('token not given but it is required')

    query = conf.get('query', '')
    return await cache.get((repo, query, token), get_latest_tag) # type: ignore

  use_latest_release = conf.get('use_latest_release', False)
  include_prereleases = conf.get('include_prereleases', False)
  if use_latest_release and include_prereleases:
    if not token:
      raise GetVersionError('token not given but it is required')

    return await cache.get((repo, token), get_latest_release_with_prereleases) # type: ignore

  br = conf.get('branch')
  path = conf.get('path')
  use_max_tag = conf.get('use_max_tag', False)
  if use_latest_release:
    url = GITHUB_LATEST_RELEASE % repo
  elif use_max_tag:
    url = GITHUB_MAX_TAG % repo
  else:
    url = GITHUB_URL % repo
    parameters = {}
    if br:
      parameters['sha'] = br
    if path:
      parameters['path'] = path
    url += '?' + urlencode(parameters)
  headers = {
    'Accept': 'application/vnd.github.quicksilver-preview+json',
  }
  if token:
    headers['Authorization'] = f'token {token}'

  data = await cache.get_json(url, headers = headers)

  if use_max_tag:
    tags: List[Union[str, RichResult]] = [
      RichResult(
        version = ref['ref'].split('/', 2)[-1],
        url = f'https://github.com/{repo}/releases/tag/{ref["ref"].split("/", 2)[-1]}',
      ) for ref in data
    ]
    if not tags:
      raise GetVersionError('No tag found in upstream repository.')
    return tags

  if use_latest_release:
    if 'tag_name' not in data:
      raise GetVersionError('No release found in upstream repository.')
    return RichResult(
      version = data['tag_name'],
      url = data['html_url'],
    )

  else:
    return RichResult(
      # YYYYMMDD.HHMMSS
      version = data[0]['commit']['committer']['date'].rstrip('Z').replace('-', '').replace(':', '').replace('T', '.'),
      url = data[0]['html_url'],
    )

def check_ratelimit(exc, name):
  res = exc.response
  if not res:
    raise

  # default -1 is used to re-raise the exception
  n = int(res.headers.get('X-RateLimit-Remaining', -1))
  if n == 0:
    reset = int(res.headers.get('X-RateLimit-Reset'))
    logger.error(f'rate limited, resetting at {time.ctime(reset)}. '
                  'Or get an API token to increase the allowance if not yet',
                 name = name,
                 reset = reset)
  else:
    raise
