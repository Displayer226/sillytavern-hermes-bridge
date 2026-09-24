const assert = require('assert');
const fs = require('fs');
const path = require('path');
const parser = require('@babel/parser');

const filename = path.resolve(__dirname, '../src/ToolCallsPanel.js');
const source = fs.readFileSync(filename, 'utf8');
const ast = parser.parse(source, {
    sourceType: 'module',
    plugins: ['jsx'],
});

const component = ast.program.body.find(
    node => node.type === 'FunctionDeclaration' && node.id?.name === 'ToolCallsPanel',
);
assert(component, 'ToolCallsPanel function was not found');

const declarationStarts = new Map();
for (const statement of component.body.body) {
    if (statement.type !== 'VariableDeclaration') continue;
    for (const declaration of statement.declarations) {
        if (declaration.id.type === 'Identifier') {
            declarationStarts.set(declaration.id.name, declaration.start);
        }
    }
}

function directHookCall(statement) {
    if (statement.type === 'ExpressionStatement' && statement.expression.type === 'CallExpression') {
        return statement.expression;
    }
    if (statement.type === 'VariableDeclaration' && statement.declarations.length === 1) {
        const init = statement.declarations[0].init;
        return init?.type === 'CallExpression' ? init : null;
    }
    return null;
}

function dependencyNames(node, names = new Set()) {
    if (!node || typeof node !== 'object') return names;
    if (node.type === 'Identifier') {
        names.add(node.name);
        return names;
    }
    if (node.type === 'MemberExpression') {
        dependencyNames(node.object, names);
        if (node.computed) dependencyNames(node.property, names);
        return names;
    }
    for (const [key, value] of Object.entries(node)) {
        if (key === 'loc' || key === 'start' || key === 'end') continue;
        if (Array.isArray(value)) value.forEach(child => dependencyNames(child, names));
        else dependencyNames(value, names);
    }
    return names;
}

const violations = [];
for (const statement of component.body.body) {
    const call = directHookCall(statement);
    if (!call || call.callee.type !== 'Identifier' || !/^use(?:Effect|LayoutEffect|Memo|Callback)$/.test(call.callee.name)) {
        continue;
    }
    const dependencies = call.arguments.at(-1);
    if (dependencies?.type !== 'ArrayExpression') continue;
    for (const name of dependencyNames(dependencies)) {
        const declarationStart = declarationStarts.get(name);
        if (declarationStart !== undefined && declarationStart > call.start) {
            violations.push(`${name} is used by ${call.callee.name} before its declaration`);
        }
    }
}

assert.deepStrictEqual(violations, []);
