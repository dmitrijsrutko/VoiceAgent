# The recognizer keeps none of a sentence the voice detector plainly heard
# (live: 5.4 s of speech, one partial "Chuck Norris。", an empty commit): the
# agent is asked with a marker and asks them to repeat, instead of silence. A
# short sound that comes to nothing is still just dropped, and so is voice that
# began while the agent was speaking: likely its own echo.
greeting Hi.
ladder off
reply caught no words = Sorry, I didn't catch that. Could you say it again?
0.2 listen
1.0 voice on
1.5 partial Chuck Norris。
3.5 voice off
5.0 final
6.2 voice on
8.2 voice off
9.0 final
12.0 voice on
12.4 voice off
14.0 final
20 end
