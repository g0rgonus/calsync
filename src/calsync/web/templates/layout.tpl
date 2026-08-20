% setdefault('title', 'calsync')
% setdefault('flash', None)
% setdefault('narrow', False)
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="same-origin">
<title>{{ title }} — calsync</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>

<header class="rail">
  <div class="rail-in">
    <a class="wordmark" href="/">calsync</a>
    <span class="wordmark-sub">console</span>
    <nav class="rail-nav">
      <a href="/">Teams</a>
      <a href="/calendar">Calendar</a>
      <a href="/onboard">Add a team</a>
      <a href="/review">Review</a>
      <a href="/venues">Venues</a>
      <a href="/household">Household</a>
      <a href="/settings">Settings</a>
    </nav>
  </div>
</header>

<main class="page {{ 'page-narrow' if narrow else '' }}">
% if flash:
  <div class="banner banner-{{ flash['kind'] }}">{{ flash['text'] }}</div>
% end
{{! base }}
</main>

</body>
</html>
