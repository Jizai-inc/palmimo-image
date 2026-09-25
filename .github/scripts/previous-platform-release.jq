[.[]
 | select(.draft == false and .prerelease == false and .tag_name != env.TAG)
 | . as $release
 | select($release.assets | any(.name == ("palmimo-platform-" + $release.tag_name + ".tar.gz")))]
| sort_by(.published_at)
| reverse
| .[0].tag_name // empty
