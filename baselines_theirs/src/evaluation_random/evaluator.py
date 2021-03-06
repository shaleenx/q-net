from collections import Counter
import json

class Evaluator(object):
    def __init__(self, path=None, articles=None, restrict_to_titles=None):
        """
        Specify either path or articles. Path should point to the JSON file
        downloaded from the website. Articles is a list of articles as found
        in the data field of the file downloaded from the website.
        
        restrict_to_titles, if specified, should be a set of article titles.
        Only articles with those titles will be used for evaluation.
        """
        assert (path is None) != (articles is None)
        if path is not None:
            with open(path, 'r') as fileobj:
                articles = json.loads(fileobj.read())['data']
        self._qas = {}
        self._answers = {}
        for article in articles:
            if restrict_to_titles is not None and article['title'] not in restrict_to_titles:
                continue
            for paragraph in article['paragraphs']:
                for qa in paragraph['qas']:
                    self._qas[qa['id']] = (article, paragraph, qa)
                    answers = self._answers[qa['id']] = []
                    for answer in qa['answers']:
                        answers.append(answer['text'])

    def GetArticleTitle(self, question_id):
        return self._qas[question_id][0]['title']
    
    def GetParagraphTextWithHighlightedAnswer(self, question_id, index=0):
        context = self._qas[question_id][1]['context']
        answer = self._qas[question_id][2]['answers'][index]
        return context[:answer['answer_start']] + '>>>>>' + answer['text'] + '<<<<<' + context[answer['answer_start'] + len(answer['text']):]

    def GetQuestionText(self, question_id):
        return self._qas[question_id][2]['question']
    
    def GetAnswerText(self, question_id, index=0):
        return self._qas[question_id][2]['answers'][index]['text']

    def ExactMatch(self, predictions):
        """
        Accepts a dict from question ID to predicted answer.
        """
        total = 0
        validCnt = 0
        for question_id in self._answers.iterkeys():
            predicted_answer = predictions.get(question_id)
            if predicted_answer is not None:
                validCnt += 1
            if predicted_answer is not None and self.ExactMatchSingle(question_id, predicted_answer):
                total += 1
        return 100.0 * total / validCnt
        # return 100.0 * total / len(self._answers)

    def ExactMatchSingle(self, question_id, predicted_answer):
        predicted_answer = Evaluator.CleanAnswer(predicted_answer)
        for answer in self._answers[question_id]:
            if Evaluator.CleanAnswer(answer) == predicted_answer:
                return True
        return False

    WHITESPACE_AND_PUNCTUATION = set([' ', '.', ',', ':', ';', '!', '?', '$', '%', '(', ')', '[', ']', '-', '`', '\'', '"'])
    ARTICLES = set(['the', 'a', 'an'])

    @staticmethod
    def CleanAnswer(answer):
        answer = answer.lower()
        if isinstance(answer, unicode):
            answer = answer.replace(u'\u00a0', ' ')
        else:
            answer = answer.replace('\xc2\xa0', ' ')
        while len(answer) > 1 and answer[0] in Evaluator.WHITESPACE_AND_PUNCTUATION:
            answer = answer[1:]
        while len(answer) > 1 and answer[-1] in Evaluator.WHITESPACE_AND_PUNCTUATION:
            answer = answer[:-1]

        answer = answer.split()
        if len(answer) > 1 and answer[0] in Evaluator.ARTICLES:
            answer = answer[1:]
        answer = ' '.join(answer)

        return answer

    def F1(self, predictions):
        """
        Accepts a dict from question ID to predicted answer.
        """
        total = 0
        validCnt = 0
        for question_id in self._answers.iterkeys():
            predicted_answer = predictions.get(question_id)
            if predicted_answer is not None:
                total += self.F1Single(question_id, predicted_answer)
                validCnt += 1
        return 100.0 * total / validCnt
        # return 100.0 * total / len(self._answers)
  
    def F1Single(self, question_id, predicted_answer):
        def GetTokens(text):
            text = Evaluator.CleanAnswer(text)
            for delimeter in Evaluator.WHITESPACE_AND_PUNCTUATION:
                text = text.replace(delimeter, ' ')
            return text.split()

        f1 = 0
        predicted_answer_tokens = Counter(GetTokens(predicted_answer))
        num_predicted_answer_tokens = sum(predicted_answer_tokens.values())
        for answer in self._answers[question_id]:
            answer_tokens = Counter(GetTokens(answer))
            num_answer_tokens = sum(answer_tokens.values())
            num_same = sum((predicted_answer_tokens & answer_tokens).values())
            if num_same == 0:
                continue
            precision = 1.0 * num_same / num_predicted_answer_tokens
            recall = 1.0 * num_same / num_answer_tokens
            f1 = max(2 * precision * recall / (precision + recall), f1)

        return f1
