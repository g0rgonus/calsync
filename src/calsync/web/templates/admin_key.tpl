% rebase('layout.tpl', title='Admin key', flash=flash, narrow=True)

<p class="eyebrow">Settings · admin key</p>
<h1>Admin key for {{ label }}</h1>
<p class="lede">
  This key creates and manages teams. It is shown <strong>once</strong>: calsync
  keeps only a hash of it.
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
      <th>Calls</th>
      <td><span class="raw">{{ prefix }}/admin/teams</span>
        <span class="choice-note" style="margin-top:0.35rem">
          On the inbound service's port. The full list is on its
          <span class="raw">{{ prefix }}/docs</span> page.
        </span>
      </td>
    </tr>
  </table>
</div>

<p class="note">
  Give it to the tool as a saved secret, not in a chat.
  <a href="/settings#admin-keys">Back to settings</a>.
</p>
