const path = require('path');
const TerserPlugin = require('terser-webpack-plugin');

module.exports = (_env, argv) => {
    const mode = argv.mode || process.env.NODE_ENV || 'development';
    const isDev = mode !== 'production';

    return {
        entry: path.join(__dirname, 'src/index.js'),
        mode,
        devtool: isDev ? 'source-map' : false,
        output: {
            path: path.join(__dirname, 'dist/'),
            filename: `index.js`,
            // Dynamic panels must get a new URL after every content change.
            // Mobile browsers/PWAs can otherwise keep an already-open copy of
            // tool-calls-panel.js even after the extension is redeployed.
            chunkFilename: '[name].[contenthash:8].js',
            uniqueName: 'responses-proxy',
            publicPath: 'auto',
            clean: true,
        },
        module: {
            rules: [
                {
                    test: /\.js/,
                    exclude: /node_modules/,
                    options: {
                        cacheDirectory: true,
                        presets: [
                            '@babel/preset-env',
                            ['@babel/preset-react', { runtime: 'automatic' }],
                        ],
                    },
                    loader: 'babel-loader',
                },
            ],
        },
        optimization: {
            minimize: true,
            minimizer: [new TerserPlugin({
                extractComments: false,
            })],
        },
    };
};
