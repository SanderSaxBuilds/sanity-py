// Records the exact HTTP requests @sanity/client sends for a fixed list of
// calls, with fetch replaced by a recorder. The Python test suite replays the
// same calls and checks that method, URL, headers and body match byte for byte.
//
//   cd tools/js-capture && npm install && node capture.mjs ../../tests/fixtures/js_requests.json

import {writeFileSync} from 'node:fs'
import {createRequire} from 'node:module'

import {createClient} from '@sanity/client'

const require = createRequire(import.meta.url)
const clientVersion = require('@sanity/client/package.json').version

const BASE = {projectId: 'abc123', dataset: 'production', apiVersion: '2025-02-19', useCdn: false}

const LONG_QUERY = `*[_type == "post" && title in $titles]{${'title, '.repeat(2000)}_id}`

const mutateResponse = {
  transactionId: 'trx-1',
  results: [
    {id: 'doc-1', operation: 'update', document: {_id: 'doc-1', _type: 'post', _rev: 'rev-1'}},
  ],
}

function responseFor(url) {
  if (url.includes('/data/query/')) return {query: '*', result: [], ms: 1}
  if (url.includes('/data/doc/')) return {documents: [{_id: 'doc-1', _type: 'post'}], omitted: []}
  if (url.includes('/data/mutate/')) return mutateResponse
  if (url.includes('/data/actions/')) return {transactionId: 'trx-1'}
  if (url.includes('/assets/')) return {document: {_id: 'image-abc-10x10-png', _type: 'sanity.imageAsset'}}
  if (url.includes('/projects')) return []
  if (url.includes('/datasets')) return []
  return {}
}

const SSE_BODY =
  'event: welcome\ndata: {"listenerName":"L1"}\nid: evt-0\n\n' +
  'event: mutation\nid: evt-1\ndata: {"documentId":"doc-1","transactionId":"trx-1","transition":"update","identity":"p-1","visibility":"query","eventId":"trx-1#doc-1","effects":{},"timestamp":"2026-11-13T00:00:00Z"}\n\n'

let recorded = []

async function bodyToRecord(body) {
  if (body === undefined || body === null) return {kind: 'none'}
  if (typeof body === 'string') return {kind: 'text', text: body}
  if (body instanceof Uint8Array) return {kind: 'bytes', base64: Buffer.from(body).toString('base64')}
  if (body instanceof ArrayBuffer) return {kind: 'bytes', base64: Buffer.from(body).toString('base64')}
  if (typeof body.getReader === 'function') {
    const chunks = []
    const reader = body.getReader()
    for (;;) {
      const {done, value} = await reader.read()
      if (done) break
      chunks.push(Buffer.from(value))
    }
    return {kind: 'bytes', base64: Buffer.concat(chunks).toString('base64')}
  }
  if (typeof body[Symbol.asyncIterator] === 'function') {
    const chunks = []
    for await (const chunk of body) chunks.push(Buffer.from(chunk))
    return {kind: 'bytes', base64: Buffer.concat(chunks).toString('base64')}
  }
  return {kind: 'unknown', text: String(body)}
}

function headersToObject(headers) {
  const out = {}
  new Headers(headers || {}).forEach((value, key) => {
    out[key] = value
  })
  return Object.fromEntries(Object.entries(out).sort(([a], [b]) => a.localeCompare(b)))
}

async function recorderFetch(input, init = {}) {
  const url = typeof input === 'string' ? input : input.url || input.href
  const method = (init.method || 'GET').toUpperCase()
  recorded.push({method, url, headers: headersToObject(init.headers), body: await bodyToRecord(init.body)})
  const accept = new Headers(init.headers || {}).get('accept') || ''
  if (accept.includes('text/event-stream')) {
    return new Response(SSE_BODY, {status: 200, headers: {'content-type': 'text/event-stream'}})
  }
  return new Response(JSON.stringify(responseFor(url)), {
    status: 200,
    headers: {'content-type': 'application/json'},
  })
}

function client(extra = {}) {
  return createClient({...BASE, ...extra, resolveFetch: () => recorderFetch})
}

function firstEvent(observable) {
  return new Promise((resolve, reject) => {
    const sub = observable.subscribe({
      next: (event) => {
        sub.unsubscribe()
        resolve(event)
      },
      error: reject,
    })
  })
}

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==',
  'base64',
)

const cases = {
  // GROQ queries
  query_simple: () => client().fetch('*[_type == "post"]'),
  query_cdn: () => client({useCdn: true}).fetch('*[_type == "post"]'),
  query_params_scalar: () =>
    client().fetch('*[_type == $type && rating > $min && featured == $flag][0...$limit]', {
      type: 'post',
      min: 3.5,
      flag: true,
      limit: 10,
    }),
  query_params_complex: () =>
    client().fetch('*[_id in $ids && category == $cat && $none == null]', {
      ids: ['a', 'b.c', 'drafts.d'],
      cat: {_ref: 'cat-1', nested: {deep: [1, 2.25, -3]}},
      none: null,
    }),
  query_params_unicode: () =>
    client().fetch('*[title match $q || author == $name]', {
      q: "naïve café ~*'()! & = + / ? #",
      name: 'Søren Ærø 日本 😀',
    }),
  query_params_numbers: () =>
    client().fetch('*[a == $a && b == $b && c == $c && d == $d && e == $e]', {
      a: 1.0,
      b: -0.5,
      c: 1e21,
      d: 123456789012,
      e: 0.1,
    }),
  query_params_undefined_dropped: () =>
    client().fetch('*[_type == $type]', {type: 'post', skip: undefined}),
  query_tag: () => client().fetch('*[_type == "post"]', {}, {tag: 'homepage'}),
  query_tag_prefix: () =>
    client({requestTagPrefix: 'website'}).fetch('*[_type == "post"]', {}, {tag: 'homepage'}),
  query_tag_prefix_only: () => client({requestTagPrefix: 'website.'}).fetch('*[_type == "post"]'),
  query_perspective_published: () =>
    client({useCdn: true, perspective: 'published'}).fetch('*[_type == "post"]'),
  query_perspective_drafts_cdn: () =>
    client({useCdn: true, perspective: 'drafts', token: 'sk-test'}).fetch('*[_type == "post"]'),
  query_perspective_raw: () => client({perspective: 'raw', token: 'sk-test'}).fetch('*'),
  query_perspective_stack: () =>
    client({useCdn: true}).fetch('*', {}, {perspective: ['rSummer', 'drafts']}),
  query_perspective_per_call: () =>
    client({perspective: 'published'}).fetch('*', {}, {perspective: 'drafts'}),
  query_token: () => client({token: 'sk-test'}).fetch('*[_type == "post"]'),
  query_filter_response_false: () =>
    client().fetch('*[_type == "post"]', {}, {filterResponse: false}),
  query_return_query_false_explicit: () =>
    client().fetch('*', {}, {filterResponse: false, returnQuery: false}),
  query_result_source_map: () =>
    client().fetch('*', {}, {resultSourceMap: 'withKeyArraySelector'}),
  query_result_source_map_true: () => client().fetch('*', {}, {resultSourceMap: true}),
  query_long_post: () => client().fetch(LONG_QUERY, {titles: ['a', 'b']}),
  query_long_post_tag: () =>
    client({perspective: 'published'}).fetch(LONG_QUERY, {titles: ['x']}, {tag: 'big'}),
  query_api_version_v_prefix: () => client({apiVersion: 'v2021-06-07'}).fetch('*'),
  query_api_version_x: () => client({apiVersion: 'X'}).fetch('*'),
  query_custom_headers: () => client({headers: {'X-Custom': 'yes'}}).fetch('*'),
  query_cdn_no_stale: () => client({useCdn: true}).fetch('*', {}, {cacheMode: 'noStale'}),
  query_last_live_event_id: () =>
    client({useCdn: true}).fetch('*', {}, {lastLiveEventId: 'live-1'}),
  query_custom_api_host: () => client({apiHost: 'https://api.example.test', useCdn: true}).fetch('*'),
  query_no_project_hostname: () =>
    client({useProjectHostname: false, apiHost: 'https://api.sanity.io'}).fetch('*'),

  // Documents
  get_document: () => client().getDocument('doc-1'),
  get_document_cdn: () => client({useCdn: true}).getDocument('doc-1'),
  get_document_tag: () => client().getDocument('drafts.doc-1', {tag: 'doc.read'}),
  get_document_all_versions: () => client().getDocument('doc-1', {includeAllVersions: true}),
  get_document_release: () => client().getDocument('doc-1', {releaseId: 'rSummer'}),
  get_documents: () => client().getDocuments(['doc-1', 'doc-2', 'drafts.doc-3']),
  get_documents_tag: () => client({token: 'sk-test'}).getDocuments(['a', 'b'], {tag: 'many'}),
  documents_exists: () => client().documentsExists(['doc-1', 'doc 2']),

  // Mutations
  create: () => client({token: 'sk-test'}).create({_type: 'post', title: 'Hello'}),
  create_options: () =>
    client({token: 'sk-test'}).create(
      {_type: 'post', title: 'Hi'},
      {returnDocuments: false, visibility: 'async', dryRun: true, autoGenerateArrayKeys: true},
    ),
  create_if_not_exists: () =>
    client({token: 'sk-test'}).createIfNotExists({_id: 'doc-1', _type: 'post', title: 'A'}),
  create_or_replace: () =>
    client({token: 'sk-test'}).createOrReplace({_id: 'doc-1', _type: 'post', title: 'B'}),
  create_or_replace_tag: () =>
    client({token: 'sk-test'}).createOrReplace(
      {_id: 'doc-1', _type: 'post'},
      {tag: 'import', visibility: 'deferred', skipCrossDatasetReferenceValidation: true},
    ),
  delete_id: () => client({token: 'sk-test'}).delete('doc-1'),
  delete_query: () =>
    client({token: 'sk-test'}).delete({query: '*[_type == $t]', params: {t: 'old'}}),
  delete_query_no_params: () => client({token: 'sk-test'}).delete({query: '*[_type == "old"]'}),
  mutate_raw: () =>
    client({token: 'sk-test'}).mutate(
      [
        {create: {_type: 'post', title: 'one'}},
        {patch: {id: 'doc-1', set: {title: 'two'}}},
        {delete: {id: 'doc-2'}},
      ],
      {returnDocuments: true, transactionId: 'my-trx'},
    ),
  mutate_return_first: () =>
    client({token: 'sk-test'}).mutate([{delete: {id: 'doc-2'}}], {returnFirst: true}),
  patch_full: () =>
    client({token: 'sk-test'})
      .patch('doc-1')
      .set({title: 'New', 'nested.prop': 1})
      .set({other: true})
      .setIfMissing({tags: []})
      .unset(['old', 'nested.gone'])
      .inc({views: 1})
      .inc({likes: 2})
      .dec({stock: 3})
      .insert('after', 'tags[-1]', ['x'])
      .ifRevisionId('rev-9')
      .commit(),
  patch_append: () => client({token: 'sk-test'}).patch('doc-1').append('tags', ['a', 'b']).commit(),
  patch_prepend: () => client({token: 'sk-test'}).patch('doc-1').prepend('tags', [{_key: 'k1'}]).commit(),
  patch_insert_replace: () =>
    client({token: 'sk-test'}).patch('doc-1').insert('replace', 'items[2]', [{n: 1}]).commit(),
  patch_insert_before: () =>
    client({token: 'sk-test'}).patch('doc-1').insert('before', 'items[0]', ['z']).commit(),
  patch_splice: () => client({token: 'sk-test'}).patch('doc-1').splice('items', 1, 2, ['q']).commit(),
  patch_splice_negative: () => client({token: 'sk-test'}).patch('doc-1').splice('items', -2, 1).commit(),
  patch_splice_all: () => client({token: 'sk-test'}).patch('doc-1').splice('items', 0).commit(),
  patch_diff_match_patch: () =>
    client({token: 'sk-test'}).patch('doc-1').diffMatchPatch({body: '@@ -1,3 +1,4 @@\n-abc\n+abcd\n'}).commit(),
  patch_query_selection: () =>
    client({token: 'sk-test'})
      .patch({query: '*[_type == "post" && views > $n]', params: {n: 10}})
      .set({popular: true})
      .commit(),
  patch_ids_selection: () =>
    client({token: 'sk-test'}).patch(['a', 'b']).unset(['flag']).commit({returnDocuments: false}),
  patch_initial_ops: () =>
    client({token: 'sk-test'}).patch('doc-1', {set: {a: 1}}).set({b: 2}).commit({visibility: 'async'}),
  patch_via_mutate: () =>
    client({token: 'sk-test'}).mutate(client().patch('doc-1').inc({n: 1})),
  transaction_full: () =>
    client({token: 'sk-test'})
      .transaction()
      .create({_type: 'post', title: 'T'})
      .createIfNotExists({_id: 'doc-2', _type: 'post'})
      .createOrReplace({_id: 'doc-3', _type: 'post', n: 1})
      .patch('doc-1', (p) => p.set({title: 'x'}).inc({n: 1}))
      .patch('doc-4', {unset: ['a']})
      .patch(client().patch('doc-5').setIfMissing({b: []}))
      .patch({query: '*[_type == "x"]'}, {set: {y: 1}})
      .delete('doc-6')
      .commit(),
  transaction_id: () =>
    client({token: 'sk-test'})
      .transaction()
      .transactionId('trx-custom')
      .delete('doc-1')
      .commit({tag: 'cleanup', visibility: 'async'}),
  transaction_return_documents: () =>
    client({token: 'sk-test'})
      .transaction([{create: {_type: 'a'}}])
      .commit({returnDocuments: true, autoGenerateArrayKeys: true}),
  transaction_via_mutate: () =>
    client({token: 'sk-test'}).mutate(client().transaction().delete('doc-1'), {dryRun: true}),
  action_single: () =>
    client({token: 'sk-test'}).action({
      actionType: 'sanity.action.document.publish',
      draftId: 'drafts.doc-1',
      publishedId: 'doc-1',
    }),
  action_many: () =>
    client({token: 'sk-test'}).action(
      [
        {actionType: 'sanity.action.document.discard', draftId: 'drafts.doc-1'},
        {actionType: 'sanity.action.document.unpublish', draftId: 'drafts.doc-2', publishedId: 'doc-2'},
      ],
      {transactionId: 'act-1', dryRun: true, tag: 'actions'},
    ),

  create_false_flags: () =>
    client({token: 'sk-test'}).create(
      {_type: 'post'},
      {dryRun: false, autoGenerateArrayKeys: false, skipCrossDatasetReferenceValidation: false},
    ),
  patch_two_inserts: () =>
    client({token: 'sk-test'}).patch('doc-1').append('a', [1]).prepend('b', [2]).commit(),
  query_preview_drafts: () =>
    client({useCdn: true, perspective: 'previewDrafts'}).fetch('*'),
  query_result_source_map_false: () => client().fetch('*', {}, {resultSourceMap: false}),
  query_long_post_unicode: () =>
    client().fetch(LONG_QUERY + ' // ø日😀', {titles: ['ü', 'x\ny', 'quote " and \\ backslash']}),
  get_document_all_versions_false: () => client().getDocument('doc-1', {includeAllVersions: false}),
  documents_exists_batched: () =>
    client().documentsExists(Array.from({length: 150}, (_, i) => `doc-${i}`)),

  // Listen
  listen_simple: () => firstEvent(client().listen('*[_type == "post"]')),
  listen_params_options: () =>
    firstEvent(
      client({token: 'sk-test'}).listen(
        '*[_type == $type]',
        {type: 'post'},
        {
          includeResult: false,
          includePreviousRevision: true,
          visibility: 'query',
          events: ['welcome', 'mutation'],
          tag: 'live',
          effectFormat: 'mendoza',
        },
      ),
    ),
  listen_all_versions: () =>
    firstEvent(
      client({requestTagPrefix: 'site'}).listen('*', {}, {includeAllVersions: true, enableResume: true, tag: 'l', events: ['welcome']}),
    ),
  listen_include_mutations_false: () =>
    firstEvent(client().listen('*', {}, {includeMutations: false, events: ['welcome']})),

  // Assets
  upload_image: () =>
    client({token: 'sk-test'}).assets.upload('image', PNG, {
      filename: 'pixel.png',
      contentType: 'image/png',
    }),
  upload_image_meta: () =>
    client({token: 'sk-test'}).assets.upload('image', PNG, {
      filename: 'pixel.png',
      contentType: 'image/png',
      extract: ['palette', 'location'],
      label: 'lbl',
      title: 'A pixel',
      description: 'One pixel',
      creditLine: 'Me & you',
      tag: 'upload',
      source: {id: 'src-1', name: 'unsplash', url: 'https://example.test/p?id=1'},
    }),
  upload_image_extract_none: () =>
    client({token: 'sk-test'}).assets.upload('image', PNG, {extract: [], contentType: 'image/png'}),
  upload_file: () =>
    client({token: 'sk-test'}).assets.upload('file', Buffer.from('hello,world\n'), {
      filename: 'data.csv',
      contentType: 'text/csv',
    }),
  upload_file_no_type: () => client({token: 'sk-test'}).assets.upload('file', Buffer.from('raw')),

  // Projects, datasets, raw requests
  projects_list: () => client({token: 'sk-test'}).projects.list(),
  projects_list_options: () =>
    client({token: 'sk-test'}).projects.list({includeMembers: false, organizationId: 'org-1'}),
  projects_get: () => client({token: 'sk-test'}).projects.getById('abc123'),
  datasets_list: () => client({token: 'sk-test'}).datasets.list(),
  request_users_me: () => client({token: 'sk-test'}).request({uri: '/users/me'}),
  request_history: () =>
    client({token: 'sk-test'}).request({
      url: '/data/history/production/documents/doc-1',
      query: {revision: 'rev-1'},
    }),
}

const out = {clientVersion, generatedBy: 'tools/js-capture/capture.mjs', cases: {}}
for (const [name, run] of Object.entries(cases)) {
  recorded = []
  try {
    await run()
  } catch (err) {
    out.cases[name] = {error: String(err && err.message), requests: recorded}
    continue
  }
  out.cases[name] = {requests: recorded}
}

const target = process.argv[2] || 'js_requests.json'
writeFileSync(target, JSON.stringify(out, null, 2) + '\n', 'utf8')
const errors = Object.entries(out.cases).filter(([, c]) => c.error)
console.log(`client ${clientVersion}: ${Object.keys(out.cases).length} cases, ${errors.length} errors`)
for (const [name, c] of errors) console.log(`  ${name}: ${c.error}`)
