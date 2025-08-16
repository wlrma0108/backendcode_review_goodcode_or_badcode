from django import forms

class ContactForm(forms.Form):  
    name = forms.CharField(max_length=50)
    email = forms.EmailField()
    message = forms.CharField(widget=forms.Textarea)

    def clean_message(self):  
        data = self.cleaned_data['message']
        if "spam" in data.lower():
            raise forms.ValidationError("스팸 메시지는 허용되지 않습니다.")
        return data
