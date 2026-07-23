import { defineConfig } from 'vite'

export default defineConfig({
  // An .ehpk is not hosted at a web origin root. Relative asset URLs are
  // required or the WebView tries to load /assets/... outside the package.
  base: './',
  server: {
    host: true,
    port: 5173,
  },
})
