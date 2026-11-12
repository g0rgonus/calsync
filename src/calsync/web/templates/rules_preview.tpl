% rebase('layout.tpl', title=activity.name + ': rules', flash=flash)

<p class="eyebrow">{{ activity.name }} · title rules</p>
<h1>Preview</h1>
<p class="lede">Nothing has changed yet. This is what Save would do.</p>

<h2>Rules, in order</h2>
<div class="card">
% if not rules:
  <p class="note" style="margin:0">No rules: every category you have not set goes to the default.</p>
% end
% for i, rule in enumerate(rules):
  <p style="margin:0 0 0.4rem">
    {{ i + 1 }}. {{ rule['described'] }} → {{ 'Skip' if rule['destination'] == 'skip' else rule['destination'] }}{{ f", at {places[rule['place']]} when the feed gives no place" if rule.get('place') in places else '' }}
  </p>
  <p class="note" style="margin:0 0 0.8rem">
% if rule['matches']:
    Matches {{ ', '.join(f'{label} ({count})' for label, count in rule['matches']) }}.
% else:
    Matches nothing in the window: waiting for a title that has not appeared, or a typo.
% end
  </p>
% end
</div>

<h2>What moves</h2>
<div class="card">
% if not moves:
  <p class="note" style="margin:0">Nothing in the sync window would move.</p>
% else:
  <table class="polls">
    <tr><th>Category</th><th>Events</th><th>From</th><th>To</th><th>Placed by</th></tr>
% for move in moves:
    <tr>
      <td>{{ move['label'] }}{{ ' (new)' if move['new'] else '' }}</td>
      <td>{{ move['count'] }}</td>
      <td>{{ move['from'] }}</td>
      <td>{{ move['to'] }}</td>
      <td class="note">{{ move['placed'] }}</td>
    </tr>
% end
  </table>
  <p class="note">{{ sum(m['count'] for m in moves if m['from'] != m['to']) }} events move on the next sync.</p>
% end
  <div class="btn-row" style="margin-top:1rem">
    <form method="post" action="/sources/{{ source.id }}/rules">
      <input type="hidden" name="rules" value="{{ rules_json }}">
      <button class="btn btn-answer" type="submit">Save</button>
    </form>
    <a class="btn btn-quiet" href="/sources/{{ source.id }}#rules">Cancel</a>
  </div>
</div>
