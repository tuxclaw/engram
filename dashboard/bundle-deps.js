// Bundle graphology deps for browser use
const { build } = require('esbuild');

build({
    stdin: {
        contents: `
            import Graph from 'graphology';
            import Sigma from 'sigma';
            import forceAtlas2 from 'graphology-layout-forceatlas2';
            import * as graphologyLayout from 'graphology-layout';
            
            window.graphology = { Graph };
            window.Sigma = Sigma;
            window.ForceAtlas2 = forceAtlas2;
            window.graphologyLayout = graphologyLayout;
        `,
        resolveDir: __dirname,
        loader: 'js',
    },
    bundle: true,
    format: 'iife',
    platform: 'browser',
    outfile: 'static/js/vendor.bundle.js',
    minify: true,
}).then(() => console.log('✅ Bundle created')).catch(e => { console.error(e); process.exit(1); });
