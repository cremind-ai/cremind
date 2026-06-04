import express from 'express'
import compression from 'compression'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const app = express()
const host = process.env.HOST || '0.0.0.0'
const port = parseInt(process.env.PORT || '1515', 10)

app.use(compression())
app.use(express.static(path.join(__dirname, '../dist-web')))

app.get('/test', (_req, res) => {
  res.json({ status: 'ok' })
})

app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, '../dist-web/index.html'))
})

app.listen(port, host, () => {
  console.log(`Server running at http://${host}:${port}`)
})
