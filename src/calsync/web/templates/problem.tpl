% rebase('layout.tpl', title=title, narrow=True)
% setdefault('detail', None)

<p class="eyebrow">Nothing was changed</p>
<h1>{{ title }}</h1>
<p class="lede">{{ message }}</p>

% if detail:
<div class="raw-block">{{ detail }}</div>
% end

<div class="btn-row" style="margin-top:1.5rem">
  <a class="btn btn-quiet" href="/">Back to teams</a>
</div>
