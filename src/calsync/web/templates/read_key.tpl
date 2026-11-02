% rebase('layout.tpl', title='Household read key', flash=flash, narrow=True)

<p class="eyebrow">Settings · household read key</p>
<h1>{{ 'New read key' if replaced else 'Read key made' }}</h1>
<p class="lede">
  This key reads every team's calendar, for every kid, and writes nothing. It is
  shown <strong>once</strong>: calsync keeps only a hash of it.
% if replaced:
  The previous read key stopped working when this one was made.
% end
</p>

<div class="card">
  <table class="derived">
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
      <td><span class="raw">GET {{ prefix }}/calendar?from=&lt;date&gt;&amp;to=&lt;date&gt;</span>
        <span class="choice-note" style="margin-top:0.35rem">
          On the inbound service's own port, not this console's. Optional
          <span class="raw">child</span> and <span class="raw">activity</span>
          narrow it to one kid or one team.
        </span>
      </td>
    </tr>
  </table>
</div>

<p class="note">
  Give it to the tool the way you gave it the team keys: not in a chat.
  <a href="/settings#read-key">Back to settings</a>.
</p>
