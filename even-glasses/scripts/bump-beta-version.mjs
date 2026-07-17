import { readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const projectDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dryRun = process.argv.includes('--dry-run')

async function readJson(filename) {
  return JSON.parse(await readFile(resolve(projectDir, filename), 'utf8'))
}

function nextPatchVersion(version) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(version)
  if (!match) {
    throw new Error(`Expected a three-part version, received "${version}"`)
  }

  return `${match[1]}.${match[2]}.${BigInt(match[3]) + 1n}`
}

const appManifest = await readJson('app.json')
const packageManifest = await readJson('package.json')
const packageLock = await readJson('package-lock.json')

const versions = [
  ['app.json', appManifest.version],
  ['package.json', packageManifest.version],
  ['package-lock.json', packageLock.version],
  ['package-lock.json root package', packageLock.packages?.['']?.version],
]
const currentVersion = versions[0][1]
const mismatched = versions.filter(([, version]) => version !== currentVersion)

if (mismatched.length > 0) {
  const details = versions.map(([source, version]) => `${source}: ${version}`).join(', ')
  throw new Error(`Version files are out of sync (${details})`)
}

const nextVersion = nextPatchVersion(currentVersion)

if (!dryRun) {
  appManifest.version = nextVersion
  packageManifest.version = nextVersion
  packageLock.version = nextVersion
  packageLock.packages[''].version = nextVersion

  await Promise.all([
    writeFile(resolve(projectDir, 'app.json'), `${JSON.stringify(appManifest, null, 2)}\n`),
    writeFile(resolve(projectDir, 'package.json'), `${JSON.stringify(packageManifest, null, 2)}\n`),
    writeFile(resolve(projectDir, 'package-lock.json'), `${JSON.stringify(packageLock, null, 2)}\n`),
  ])
}

console.log(`${dryRun ? 'Would bump' : 'Bumped'} beta version ${currentVersion} -> ${nextVersion}`)
