// Prints how @sanity/client formats a GROQ parse error from the live API.
//   node probe_error.mjs '<query>'
import {createClient} from '@sanity/client'

const query = process.argv[2] || '*[_type == "movie" && ]'
const client = createClient({projectId: 'zp7mbokg', dataset: 'production', apiVersion: '2025-02-19', useCdn: true})
try {
  await client.fetch(query)
} catch (err) {
  console.log(JSON.stringify({status: err.statusCode, body: err.response && err.response.body}, null, 2))
  console.log('---- message ----')
  console.log(err.message)
}
