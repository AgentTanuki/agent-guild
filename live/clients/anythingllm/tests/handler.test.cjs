const { test } = require('node:test');
const { exercise } = require('./exercise-handler.cjs');
const { runtime } = require('../agent-guild-passport/handler.js');
exercise(test, (runtimeArgs) => ({ ...runtime, runtimeArgs }));
