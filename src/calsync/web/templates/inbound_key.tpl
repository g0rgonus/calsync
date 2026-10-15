% rebase('layout.tpl', title='API key — ' + activity.name, flash=flash, narrow=True)

<p class="eyebrow">{{ child.name }} · {{ activity.sport }} · <span style="text-transform:none;letter-spacing:0">{{ source.id }}</span></p>
<h1>{{ 'Created and staged' if fresh else 'New key' }}</h1>
<p class="lede">
  Give the tool that sends {{ activity.name }}'s schedule these two things. The
  key is shown <strong>once</strong>: calsync keeps only a hash of it, so if it
  is lost, make a new one from the team's page.
% if not fresh:
  The previous key stopped working when this one was made.
% end
</p>

<div class="card">
  <table class="derived">
    <tr>
      <th>Source id</th>
      <td><span class="raw">{{ source.id }}</span></td>
    </tr>
    <tr>
      <th>Key</th>
      <td><span class="raw" style="user-select:all;word-break:break-all">{{ key }}</span></td>
    </tr>
    <tr>
      <th>Header</th>
      <td><span class="raw">{{ header }}: &lt;key&gt;</span></td>
    </tr>
    <tr>
      <th>Endpoint</th>
      <td><span class="raw">{{ prefix }}/sources/{{ source.id }}/events/&lt;your-id&gt;</span>
        <span class="choice-note" style="margin-top:0.35rem">
          On the inbound service's own port ({{ port }} by default), not on this
          console's. <code>{{ prefix }}/docs</code> there is a page describing every
          call with examples, and <code>{{ prefix }}/openapi.json</code> is the spec to
          give an assistant that imports one.
        </span>
      </td>
    </tr>
  </table>
</div>

<h2>Try it</h2>
<div class="card">
  <p class="note">
    Through a proxy that wants its own login, add it as you normally would
    (<code>-u user:pass</code> for Pangolin's Basic Header Auth). calsync never
    reads <code>Authorization</code>, so the two do not collide.
  </p>
  <div class="raw-block" style="white-space:pre-wrap;word-break:break-all">curl -X PUT https://&lt;host&gt;{{ prefix }}/sources/{{ source.id }}/events/tue-practice \
  -H '{{ header }}: {{ key }}' \
  -H 'Content-Type: application/json' \
  -d '{"type": "practice", "label": "Practice",
       "start": "2026-10-20T17:45:00", "end": "2026-10-20T19:30:00",
       "rrule": "FREQ=WEEKLY;BYDAY=TU;UNTIL=20270531T235959Z"}'</div>
</div>

<h2>What happens next</h2>
<div class="card">
  <p class="note" style="margin:0">
    Whatever the tool sends goes to the staging calendar first, exactly like a
    new feed. Look at it on your phone, then promote it from
    <a href="/sources/{{ source.id }}">the team's page</a>. The title, the
    calendar it lands in, alarms and warm-ups all come from calsync's own
    settings — the tool describes events, it does not get to word them.
  </p>
</div>
