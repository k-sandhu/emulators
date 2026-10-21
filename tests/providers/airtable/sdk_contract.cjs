// The unmodified official airtable.js client talks to a real local gateway socket.
const assert = require('node:assert/strict');
const path = require('node:path');
const packagePath = path.join(process.env.AIRTABLE_SDK_NODE_MODULES, 'airtable');
const Airtable = require(packagePath);
assert.equal(require(path.join(packagePath, 'package.json')).version, '0.12.2');

async function main() {
    const api = new Airtable({apiKey: 'pat-airtable-owner', endpointUrl: process.env.AIRTABLE_EMULATOR_URL,
        noRetryIfRateLimited: true, requestTimeout: 10000});
    const table = api.base(process.env.AIRTABLE_BASE_ID)('Clients');
    const paged = await table.select({pageSize: 1, maxRecords: 3, sort: [{field: 'Name'}]}).all();
    assert.equal(paged.length, 3);
    assert.equal(new Set(paged.map(r => r.id)).size, 3);
    const selected = await table.select({filterByFormula: "{Name}='Northwind Logistics'", fields: ['Name']}).firstPage();
    assert.equal(selected.length, 1);
    assert.equal(selected[0].get('Name'), 'Northwind Logistics');
    const fieldIds = await table.select({returnFieldsByFieldId: true, maxRecords: 1}).all();
    assert.ok(Object.keys(fieldIds[0].fields).every(key => key.startsWith('fld')));
    const created = await table.create({Name: 'Official JS client', Notes: 'Preserve on PATCH', Status: 'Prospect'});
    assert.ok(/^rec[a-zA-Z0-9]{14}$/.test(created.id));
    const fetched = await table.find(created.id);
    assert.equal(fetched.get('Notes'), 'Preserve on PATCH');
    const updated = await table.update(created.id, {Name: 'JS updated'});
    assert.equal(updated.get('Notes'), 'Preserve on PATCH');
    const replaced = await table.replace(created.id, {Name: 'JS replacement'});
    assert.deepEqual(replaced.fields, {Name: 'JS replacement'});
    const batch = await table.create([{fields: {Name: 'JS batch A'}}, {fields: {Name: 'JS batch B'}}]);
    const updatedBatch = await table.update(batch.map(r => ({id: r.id, fields: {Notes: 'Batch edit'}})));
    assert.equal(updatedBatch.length, 2);
    assert.ok(updatedBatch.every(r => r.get('Notes') === 'Batch edit'));
    const removed = await table.destroy(batch.map(r => r.id));
    assert.equal(removed.length, 2);
    await table.destroy(created.id);
    await assert.rejects(() => table.find(created.id), error => error.statusCode === 404 && error.error === 'NOT_FOUND');
    await assert.rejects(() => table.create({Status: 'not a choice'}), error => error.statusCode === 422 && error.error === 'INVALID_MULTIPLE_CHOICE_OPTIONS');
    console.log(JSON.stringify({sdk: 'airtable', version: '0.12.2', assertions: 19}));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
